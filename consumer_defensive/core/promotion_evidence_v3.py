"""Anchored fresh-evidence authorization for Consumer Defensive promotion v3.

The promotion input may *request* ``fresh_chronological`` treatment, but only
this module can authorize it.  Authorization requires a review plan frozen
before the window, an externally pinned registration anchor, and an exact
append-only extension of both realized daily paths and outer-OOS identities.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from consumer_defensive.core.promotion_engine_v3 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    framework_sha256,
    validate_promotion_input,
    value_sha256,
)


PREREGISTRATION_SCHEMA = "consumer_defensive_promotion_review_preregistration_v3"
ANCHOR_SCHEMA = "consumer_defensive_promotion_registration_anchor_v3"
FRESH_MANIFEST_SCHEMA = "consumer_defensive_fresh_evidence_manifest_v3"
MODEL_FAMILY = "consumer_defensive"
HORIZON_KEYS = frozenset(str(value) for value in REQUIRED_HORIZONS)
SHA_LENGTH = 64


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _exact(value: Any, keys: frozenset[str], *, label: str) -> dict[str, Any]:
    result = _mapping(value, label=label)
    if set(result) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return result


def _sha(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != SHA_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _iso_date(value: Any, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} must be UTC")
    canonical = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    if canonical != value:
        raise ValueError(f"{label} must use second-resolution canonical UTC")
    return parsed


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a nonblank canonical identifier")
    return value


def _date_list(
    value: Any,
    *,
    label: str,
    after: date,
    on_or_before: date,
    allow_empty: bool = False,
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence of ISO dates")
    parsed = [_iso_date(item, label=f"{label} date") for item in value]
    if (not parsed and not allow_empty) or parsed != sorted(set(parsed)):
        raise ValueError(f"{label} must be nonempty, increasing, and unique")
    if any(item <= after or item > on_or_before for item in parsed):
        raise ValueError(f"{label} dates fall outside the preregistered window")
    return [item.isoformat() for item in parsed]


def _methodology_hashes(value: Any) -> dict[str, str]:
    result = _mapping(value, label="methodology_file_sha256s")
    if not result:
        raise ValueError("methodology_file_sha256s cannot be empty")
    normalized: dict[str, str] = {}
    for name, digest in sorted(result.items()):
        normalized[_identifier(name, label="methodology file name")] = _sha(
            digest, label=f"methodology hash {name}"
        )
    return normalized


def _contract_hashes(promotion_input: Mapping[str, Any]) -> dict[str, str]:
    return {
        cohort: str(
            promotion_input["cohorts"][cohort]["production_model_contract"][
                "payload_sha256"
            ]
        )
        for cohort in sorted(REQUIRED_COHORTS)
    }


def build_review_preregistration(
    *,
    review_id: str,
    registered_at_utc: str,
    fresh_start_exclusive: str,
    scheduled_decision_asof: str,
    eligible_return_dates: Sequence[str],
    eligible_outer_oos_dates_by_cohort_horizon: Mapping[
        str, Mapping[str, Sequence[str]]
    ],
    minimum_new_paired_observations_by_horizon: Mapping[str, int],
    methodology_file_sha256s: Mapping[str, str],
    framework: Mapping[str, Any],
    previous_decision: Mapping[str, Any],
    previous_promotion_input: Mapping[str, Any],
    trusted_previous_decision_sha256: str,
) -> dict[str, Any]:
    previous = validate_promotion_input(previous_promotion_input, framework=framework)
    trusted_previous = _sha(
        trusted_previous_decision_sha256,
        label="trusted previous decision hash",
    )
    observed_previous = _sha(
        previous_decision.get("payload_sha256"), label="previous decision hash"
    )
    if observed_previous != trusted_previous:
        raise ValueError("previous decision does not match its trusted digest")
    if previous_decision.get("source_input_sha256") != previous["payload_sha256"]:
        raise ValueError("previous decision is not bound to the baseline promotion input")
    start = _iso_date(fresh_start_exclusive, label="fresh_start_exclusive")
    scheduled = _iso_date(scheduled_decision_asof, label="scheduled_decision_asof")
    if scheduled <= start:
        raise ValueError("scheduled decision must follow the fresh-window boundary")
    registered = _utc(registered_at_utc, label="registered_at_utc")
    if registered.date() > start:
        raise ValueError("review plan must be registered no later than the window boundary")
    returns = _date_list(
        eligible_return_dates,
        label="eligible_return_dates",
        after=start,
        on_or_before=scheduled,
    )
    raw_outer = _mapping(
        eligible_outer_oos_dates_by_cohort_horizon,
        label="eligible_outer_oos_dates_by_cohort_horizon",
    )
    if set(raw_outer) != REQUIRED_COHORTS:
        raise ValueError("outer-OOS date plan must cover exactly four cohorts")
    outer: dict[str, dict[str, list[str]]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        horizons = _mapping(raw_outer[cohort], label=f"outer dates {cohort}")
        if set(horizons) != HORIZON_KEYS:
            raise ValueError(f"{cohort}: outer date plan requires 21/63/126")
        outer[cohort] = {
            key: _date_list(
                horizons[key],
                label=f"outer dates {cohort}/{key}",
                after=start,
                on_or_before=scheduled,
            )
            for key in sorted(HORIZON_KEYS, key=int)
        }
    minimum = _mapping(
        minimum_new_paired_observations_by_horizon,
        label="minimum_new_paired_observations_by_horizon",
    )
    if set(minimum) != HORIZON_KEYS or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in minimum.values()
    ):
        raise ValueError("minimum new observations must be positive integers for 21/63/126")
    methodology = _methodology_hashes(methodology_file_sha256s)
    methodology_hash = value_sha256(methodology)
    if previous["source_lineage"]["code_sha256"] != methodology_hash:
        raise ValueError("baseline code lineage does not match frozen methodology files")
    baseline_census = {
        cohort: {
            key: {
                "path_end_date": previous["cohorts"][cohort]["horizons"][key][
                    "daily_path"
                ][-1]["date"],
                "path_sha256": value_sha256(
                    previous["cohorts"][cohort]["horizons"][key]["daily_path"]
                ),
                "outer_oos_observation_count": len(
                    previous["cohorts"][cohort]["horizons"][key][
                        "outer_oos_observations"
                    ]
                ),
                "outer_oos_observation_ids_sha256": value_sha256(
                    [
                        row["observation_id"]
                        for row in previous["cohorts"][cohort]["horizons"][key][
                            "outer_oos_observations"
                        ]
                    ]
                ),
            }
            for key in sorted(HORIZON_KEYS, key=int)
        }
        for cohort in sorted(REQUIRED_COHORTS)
    }
    plan: dict[str, Any] = {
        "schema_version": PREREGISTRATION_SCHEMA,
        "model_family": MODEL_FAMILY,
        "review_id": _identifier(review_id, label="review_id"),
        "registered_at_utc": registered_at_utc,
        "framework_sha256": framework_sha256(framework),
        "previous_decision_sha256": trusted_previous,
        "previous_promotion_input_sha256": previous["payload_sha256"],
        "review_window": {
            "fresh_start_exclusive": start.isoformat(),
            "scheduled_decision_asof": scheduled.isoformat(),
            "eligible_return_dates": returns,
            "eligible_outer_oos_dates_by_cohort_horizon": outer,
            "minimum_new_paired_observations_by_horizon": {
                key: int(minimum[key]) for key in sorted(HORIZON_KEYS, key=int)
            },
        },
        "baseline_census": baseline_census,
        "production_model_contract_sha256_by_cohort": _contract_hashes(previous),
        "candidate_registry_sha256": previous["source_lineage"][
            "candidate_registry_sha256"
        ],
        "methodology_file_sha256s": methodology,
        "methodology_sha256": methodology_hash,
        "registration_requires_trusted_anchor": True,
        "production_promotion_enabled": False,
        "portfolio_write_enabled": False,
    }
    plan["payload_sha256"] = canonical_sha256(plan)
    return validate_review_preregistration(plan, framework=framework)


def validate_review_preregistration(
    payload: Mapping[str, Any], *, framework: Mapping[str, Any]
) -> dict[str, Any]:
    keys = frozenset(
        {
            "schema_version",
            "model_family",
            "review_id",
            "registered_at_utc",
            "framework_sha256",
            "previous_decision_sha256",
            "previous_promotion_input_sha256",
            "review_window",
            "baseline_census",
            "production_model_contract_sha256_by_cohort",
            "candidate_registry_sha256",
            "methodology_file_sha256s",
            "methodology_sha256",
            "registration_requires_trusted_anchor",
            "production_promotion_enabled",
            "portfolio_write_enabled",
            "payload_sha256",
        }
    )
    plan = _exact(payload, keys, label="promotion review preregistration v3")
    if plan["schema_version"] != PREREGISTRATION_SCHEMA or plan["model_family"] != MODEL_FAMILY:
        raise ValueError("unsupported promotion review preregistration")
    _identifier(plan["review_id"], label="review_id")
    registered = _utc(plan["registered_at_utc"], label="registered_at_utc")
    if plan["framework_sha256"] != framework_sha256(framework):
        raise ValueError("preregistration is bound to a different framework")
    for field in (
        "previous_decision_sha256",
        "previous_promotion_input_sha256",
        "candidate_registry_sha256",
        "methodology_sha256",
    ):
        _sha(plan[field], label=field)
    window = _exact(
        plan["review_window"],
        frozenset(
            {
                "fresh_start_exclusive",
                "scheduled_decision_asof",
                "eligible_return_dates",
                "eligible_outer_oos_dates_by_cohort_horizon",
                "minimum_new_paired_observations_by_horizon",
            }
        ),
        label="review_window",
    )
    start = _iso_date(window["fresh_start_exclusive"], label="fresh_start_exclusive")
    scheduled = _iso_date(window["scheduled_decision_asof"], label="scheduled_decision_asof")
    if registered.date() > start or scheduled <= start:
        raise ValueError("preregistration timing is invalid")
    _date_list(
        window["eligible_return_dates"],
        label="eligible_return_dates",
        after=start,
        on_or_before=scheduled,
    )
    outer = _mapping(
        window["eligible_outer_oos_dates_by_cohort_horizon"],
        label="eligible outer dates",
    )
    baseline = _mapping(plan["baseline_census"], label="baseline_census")
    contracts = _mapping(
        plan["production_model_contract_sha256_by_cohort"],
        label="production model contracts",
    )
    if set(outer) != REQUIRED_COHORTS or set(baseline) != REQUIRED_COHORTS or set(contracts) != REQUIRED_COHORTS:
        raise ValueError("preregistration cohort census changed")
    for cohort in sorted(REQUIRED_COHORTS):
        horizon_dates = _mapping(outer[cohort], label=f"outer dates {cohort}")
        horizon_baseline = _mapping(baseline[cohort], label=f"baseline {cohort}")
        if set(horizon_dates) != HORIZON_KEYS or set(horizon_baseline) != HORIZON_KEYS:
            raise ValueError(f"{cohort}: preregistration horizon census changed")
        _sha(contracts[cohort], label=f"contract hash {cohort}")
        for key in sorted(HORIZON_KEYS, key=int):
            _date_list(
                horizon_dates[key],
                label=f"outer dates {cohort}/{key}",
                after=start,
                on_or_before=scheduled,
            )
            cell = _exact(
                horizon_baseline[key],
                frozenset(
                    {
                        "path_end_date",
                        "path_sha256",
                        "outer_oos_observation_count",
                        "outer_oos_observation_ids_sha256",
                    }
                ),
                label=f"baseline {cohort}/{key}",
            )
            _iso_date(cell["path_end_date"], label="baseline path end")
            _sha(cell["path_sha256"], label="baseline path hash")
            _sha(cell["outer_oos_observation_ids_sha256"], label="baseline IDs hash")
            if isinstance(cell["outer_oos_observation_count"], bool) or not isinstance(
                cell["outer_oos_observation_count"], int
            ) or cell["outer_oos_observation_count"] < 1:
                raise ValueError("baseline observation count must be positive")
    minimum = _mapping(
        window["minimum_new_paired_observations_by_horizon"],
        label="minimum new observations",
    )
    if set(minimum) != HORIZON_KEYS or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in minimum.values()
    ):
        raise ValueError("minimum new observation policy is invalid")
    methodology = _methodology_hashes(plan["methodology_file_sha256s"])
    if value_sha256(methodology) != plan["methodology_sha256"]:
        raise ValueError("methodology aggregate hash mismatch")
    if plan["registration_requires_trusted_anchor"] is not True:
        raise ValueError("fresh review must require a trusted registration anchor")
    if plan["production_promotion_enabled"] is not False or plan["portfolio_write_enabled"] is not False:
        raise ValueError("preregistration is report-only")
    if canonical_sha256(plan) != _sha(plan["payload_sha256"], label="preregistration hash"):
        raise ValueError("preregistration self-hash mismatch")
    return plan


def build_registration_anchor(
    *,
    preregistration: Mapping[str, Any],
    framework: Mapping[str, Any],
    anchor_created_at_utc: str,
    registration_authority: str,
    anchor_id: str,
) -> dict[str, Any]:
    plan = validate_review_preregistration(preregistration, framework=framework)
    created = _utc(anchor_created_at_utc, label="anchor_created_at_utc")
    registered = _utc(plan["registered_at_utc"], label="registered_at_utc")
    start = _iso_date(
        plan["review_window"]["fresh_start_exclusive"],
        label="fresh_start_exclusive",
    )
    if created < registered or created.date() > start:
        raise ValueError("anchor must be created after registration and before fresh access")
    anchor: dict[str, Any] = {
        "schema_version": ANCHOR_SCHEMA,
        "model_family": MODEL_FAMILY,
        "review_id": plan["review_id"],
        "preregistration_sha256": plan["payload_sha256"],
        "registered_at_utc": plan["registered_at_utc"],
        "anchor_created_at_utc": anchor_created_at_utc,
        "registration_authority": _identifier(
            registration_authority, label="registration_authority"
        ),
        "anchor_id": _identifier(anchor_id, label="anchor_id"),
    }
    anchor["payload_sha256"] = canonical_sha256(anchor)
    return anchor


def validate_registration_anchor(
    payload: Mapping[str, Any],
    *,
    preregistration: Mapping[str, Any],
    framework: Mapping[str, Any],
    trusted_anchor_sha256: str,
) -> dict[str, Any]:
    plan = validate_review_preregistration(preregistration, framework=framework)
    anchor = _exact(
        payload,
        frozenset(
            {
                "schema_version",
                "model_family",
                "review_id",
                "preregistration_sha256",
                "registered_at_utc",
                "anchor_created_at_utc",
                "registration_authority",
                "anchor_id",
                "payload_sha256",
            }
        ),
        label="promotion registration anchor v3",
    )
    if anchor["schema_version"] != ANCHOR_SCHEMA or anchor["model_family"] != MODEL_FAMILY:
        raise ValueError("unsupported promotion registration anchor")
    if (
        anchor["review_id"] != plan["review_id"]
        or anchor["preregistration_sha256"] != plan["payload_sha256"]
        or anchor["registered_at_utc"] != plan["registered_at_utc"]
    ):
        raise ValueError("registration anchor is not bound to the review plan")
    _identifier(anchor["registration_authority"], label="registration_authority")
    _identifier(anchor["anchor_id"], label="anchor_id")
    registered = _utc(anchor["registered_at_utc"], label="registered_at_utc")
    created = _utc(anchor["anchor_created_at_utc"], label="anchor_created_at_utc")
    start = _iso_date(plan["review_window"]["fresh_start_exclusive"], label="fresh start")
    if created < registered or created.date() > start:
        raise ValueError("registration anchor timing is invalid")
    observed = _sha(anchor["payload_sha256"], label="registration anchor hash")
    if canonical_sha256(anchor) != observed:
        raise ValueError("registration anchor self-hash mismatch")
    if observed != _sha(trusted_anchor_sha256, label="trusted anchor hash"):
        raise ValueError("registration anchor does not match the external trusted digest")
    return anchor


def _extension_cell(
    *,
    prior_horizon: Mapping[str, Any],
    current_horizon: Mapping[str, Any],
    eligible_return_dates: Sequence[str],
    eligible_outer_dates: Sequence[str],
    minimum_new: int,
    label: str,
) -> dict[str, Any]:
    prior_path = list(prior_horizon["daily_path"])
    current_path = list(current_horizon["daily_path"])
    if len(current_path) <= len(prior_path) or current_path[: len(prior_path)] != prior_path:
        raise ValueError(f"{label}: daily path is not an exact strict prefix extension")
    new_path = current_path[len(prior_path) :]
    new_dates = [row["date"] for row in new_path]
    if new_dates != list(eligible_return_dates):
        raise ValueError(f"{label}: new daily dates differ from the preregistered census")
    prior_outer = list(prior_horizon["outer_oos_observations"])
    current_outer = list(current_horizon["outer_oos_observations"])
    if len(current_outer) <= len(prior_outer) or current_outer[: len(prior_outer)] != prior_outer:
        raise ValueError(f"{label}: outer-OOS identities are not an exact strict extension")
    new_outer = current_outer[len(prior_outer) :]
    new_outer_dates = [row["signal_date"] for row in new_outer]
    if new_outer_dates != list(eligible_outer_dates):
        raise ValueError(f"{label}: outer-OOS dates differ from the preregistered census")
    if len(new_outer) < minimum_new:
        raise ValueError(f"{label}: insufficient new paired observations")
    if int(current_horizon["performance"]["paired_observation_count"]) != len(current_outer):
        raise ValueError(f"{label}: performance count does not match outer-OOS identities")
    return {
        "prior_path_sha256": value_sha256(prior_path),
        "current_path_sha256": value_sha256(current_path),
        "prior_end_date": prior_path[-1]["date"],
        "current_end_date": current_path[-1]["date"],
        "new_daily_date_count": len(new_dates),
        "new_daily_dates_sha256": value_sha256(new_dates),
        "prior_outer_oos_observation_ids_sha256": value_sha256(
            [row["observation_id"] for row in prior_outer]
        ),
        "current_outer_oos_observation_ids_sha256": value_sha256(
            [row["observation_id"] for row in current_outer]
        ),
        "new_outer_oos_observation_ids_sha256": value_sha256(
            [row["observation_id"] for row in new_outer]
        ),
        "new_paired_observation_count": len(new_outer),
    }


def build_fresh_evidence_manifest(
    *,
    preregistration: Mapping[str, Any],
    registration_anchor: Mapping[str, Any],
    trusted_anchor_sha256: str,
    previous_decision: Mapping[str, Any],
    previous_promotion_input: Mapping[str, Any],
    current_promotion_input: Mapping[str, Any],
    framework: Mapping[str, Any],
) -> dict[str, Any]:
    plan = validate_review_preregistration(preregistration, framework=framework)
    anchor = validate_registration_anchor(
        registration_anchor,
        preregistration=plan,
        framework=framework,
        trusted_anchor_sha256=trusted_anchor_sha256,
    )
    previous = validate_promotion_input(previous_promotion_input, framework=framework)
    current = validate_promotion_input(current_promotion_input, framework=framework)
    if current["evidence_role"] != "fresh_chronological":
        raise ValueError("current input did not request fresh chronological treatment")
    if (
        previous["payload_sha256"] != plan["previous_promotion_input_sha256"]
        or previous_decision.get("payload_sha256") != plan["previous_decision_sha256"]
        or previous_decision.get("source_input_sha256") != previous["payload_sha256"]
    ):
        raise ValueError("baseline decision/input do not match the preregistration")
    window = plan["review_window"]
    if current["asof_date"] != window["scheduled_decision_asof"]:
        raise ValueError("current input is not the preregistered decision snapshot")
    if current["framework_sha256"] != plan["framework_sha256"]:
        raise ValueError("current input changed promotion framework")
    if current["source_lineage"]["code_sha256"] != plan["methodology_sha256"]:
        raise ValueError("current input changed frozen methodology code")
    if current["source_lineage"]["candidate_registry_sha256"] != plan[
        "candidate_registry_sha256"
    ]:
        raise ValueError("current input changed the candidate registry")
    if _contract_hashes(current) != plan["production_model_contract_sha256_by_cohort"]:
        raise ValueError("current input changed a production model contract")
    if not all(current["safety_attestations"].values()):
        raise ValueError("fresh evidence cannot authorize with failed safety attestations")
    cohort_evidence: dict[str, Any] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        cohort_evidence[cohort] = {}
        for key in sorted(HORIZON_KEYS, key=int):
            cohort_evidence[cohort][key] = _extension_cell(
                prior_horizon=previous["cohorts"][cohort]["horizons"][key],
                current_horizon=current["cohorts"][cohort]["horizons"][key],
                eligible_return_dates=window["eligible_return_dates"],
                eligible_outer_dates=window[
                    "eligible_outer_oos_dates_by_cohort_horizon"
                ][cohort][key],
                minimum_new=int(
                    window["minimum_new_paired_observations_by_horizon"][key]
                ),
                label=f"{cohort}/{key}",
            )
    manifest: dict[str, Any] = {
        "schema_version": FRESH_MANIFEST_SCHEMA,
        "model_family": MODEL_FAMILY,
        "review_id": plan["review_id"],
        "preregistration_sha256": plan["payload_sha256"],
        "registration_anchor_sha256": anchor["payload_sha256"],
        "asof_date": current["asof_date"],
        "framework_sha256": current["framework_sha256"],
        "previous_decision_sha256": plan["previous_decision_sha256"],
        "previous_promotion_input_sha256": previous["payload_sha256"],
        "current_promotion_input_sha256": current["payload_sha256"],
        "methodology_file_sha256s": plan["methodology_file_sha256s"],
        "methodology_sha256": plan["methodology_sha256"],
        "production_model_contract_sha256_by_cohort": _contract_hashes(current),
        "cohorts": cohort_evidence,
        "computed_safety_attestations": dict(current["safety_attestations"]),
        "freshness_status": "verified_fresh_chronological",
        "calibration_write_performed": False,
        "portfolio_write_performed": False,
    }
    manifest["payload_sha256"] = canonical_sha256(manifest)
    return manifest


def validate_fresh_evidence_manifest(
    payload: Mapping[str, Any],
    *,
    preregistration: Mapping[str, Any],
    registration_anchor: Mapping[str, Any],
    trusted_anchor_sha256: str,
    previous_decision: Mapping[str, Any],
    previous_promotion_input: Mapping[str, Any],
    current_promotion_input: Mapping[str, Any],
    framework: Mapping[str, Any],
) -> dict[str, Any]:
    actual = _mapping(payload, label="fresh evidence manifest v3")
    if actual.get("schema_version") != FRESH_MANIFEST_SCHEMA:
        raise ValueError("unsupported fresh evidence manifest")
    observed = _sha(actual.get("payload_sha256"), label="fresh manifest hash")
    if canonical_sha256(actual) != observed:
        raise ValueError("fresh evidence manifest self-hash mismatch")
    expected = build_fresh_evidence_manifest(
        preregistration=preregistration,
        registration_anchor=registration_anchor,
        trusted_anchor_sha256=trusted_anchor_sha256,
        previous_decision=previous_decision,
        previous_promotion_input=previous_promotion_input,
        current_promotion_input=current_promotion_input,
        framework=framework,
    )
    if actual != expected:
        raise ValueError("fresh evidence manifest does not reproduce")
    return actual


__all__ = [
    "ANCHOR_SCHEMA",
    "FRESH_MANIFEST_SCHEMA",
    "PREREGISTRATION_SCHEMA",
    "build_fresh_evidence_manifest",
    "build_registration_anchor",
    "build_review_preregistration",
    "validate_fresh_evidence_manifest",
    "validate_registration_anchor",
    "validate_review_preregistration",
]

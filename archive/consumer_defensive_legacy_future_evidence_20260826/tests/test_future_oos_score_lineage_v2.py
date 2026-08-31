from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from consumer_defensive.core import future_oos_score_lineage_v2 as lineage
from consumer_defensive.core.future_oos_capture_v4 import RANK_SNAPSHOT_SCHEMA
from consumer_defensive.core.scoring_features import (
    CORE_COMPONENT_SPECS,
    component_observation_id,
    input_observation_id,
)
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.score_input_availability import (
    SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
    SCORE_INPUT_AVAILABILITY_POLICY,
    SCORE_INPUT_AVAILABILITY_SCHEMA,
)


ASOF = "2026-09-30"
COHORT = "beverages"
STAGE6_HASH = "a" * 64
STAGE7_HASH = "b" * 64
STAGE6_SOURCE = "consumer_stage6_atomic_test"
SIGNAL_CUTOFF = "2026-09-30T20:00:00+00:00"
POLICY_ID = "consumer-test-policy"


class _Authority:
    def verify_snapshot(
        self,
        payload_bytes: bytes,
        digest: str,
        payload: dict[str, object],
    ) -> bool:
        assert hashlib.sha256(payload_bytes).hexdigest() == digest
        assert json.loads(payload_bytes.decode("utf-8")) == payload
        return True


def _bundle() -> SimpleNamespace:
    return SimpleNamespace(
        market_data_export=_Authority(),
        allowed_provider_ids=frozenset({"provider"}),
        allowed_dataset_ids=frozenset({"consumer-score-inputs"}),
    )


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _weights(total: float = 1.0) -> dict[str, float]:
    weight = total / len(CORE_COMPONENT_SPECS)
    return {spec.name: weight for spec in CORE_COMPONENT_SPECS}


def _baseline() -> dict[str, object]:
    core_weights = _weights()
    return {
        "schema_version": lineage.BASELINE_SPEC_SCHEMA,
        "score_formula_id": lineage.SCORE_FORMULA_ID,
        "quality_gate_arithmetic": lineage.QUALITY_GATE_ARITHMETIC,
        "specialized_scoring_policy": lineage.SPECIALIZED_SCORING_POLICY,
        "neutral_score": 50.0,
        "minimum_data_quality_confidence": 0.80,
        "maximum_missing_component_weight": 0.20,
        "minimum_normalization_peer_count": 2,
        "stage6_contract_sha256": STAGE6_HASH,
        "stage6_definition_version": "consumer_stage6_contract_test_v1",
        "stage6_input_source_id": STAGE6_SOURCE,
        "stage7_model_contract_sha256": STAGE7_HASH,
        "stage7_core_weights": core_weights,
        "stage7_output_contract": {
            "source_id": "consumer_stage7_test",
            "baseline_source_id": STAGE6_SOURCE,
            "model_version": "consumer_stage7_test_v1",
            "promotion_state": "shadow_only",
            "portfolio_candidate_gate": 0,
            "oos_score_valid_flag": 0,
            "specialized_weight_policy": lineage.SPECIALIZED_SCORING_POLICY,
            "factor_validation_campaign_id": "none_future_frozen",
            "factor_validation_verdict": "zero_weight",
        },
        "specialized_component_names": [],
        "cohort_models": {
            COHORT: {
                "candidate_id": "beverages_frozen_candidate",
                "core_weights": core_weights,
                "specialized_weights": {},
            }
        },
    }


def _component_row(
    *,
    ticker: str,
    ticker_index: int,
    spec_index: int,
) -> dict[str, object]:
    spec = CORE_COMPONENT_SPECS[spec_index]
    raw_value = float(ticker_index + 1)
    higher_score = float(ticker_index * 100)
    score = 100.0 - higher_score if spec.direction == "lower" else higher_score
    row: dict[str, object] = {
        "ticker": ticker,
        "asof_date": ASOF,
        "component_name": spec.name,
        "raw_value": raw_value,
        "normalized_value": score,
        "component_score": score,
        "component_weight": 0.0,
        "availability_status": "available",
        "source_asof_date": ASOF,
        "quality_status": "accepted",
        "component_group": spec.group,
        "direction": spec.direction,
        "rank_requirement": spec.rank_requirement,
        "unit": spec.unit,
        "definition_version": "consumer_stage6_contract_test_v1",
        "contract_sha256": STAGE6_HASH,
        "source_id": STAGE6_SOURCE,
        "source_table": spec.source_table,
        "source_field": spec.source_field,
        "exclusion_reason": None,
        "lineage_json": json.dumps(
            {"normalization_scope": "cohort"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "production_status": "shadow_measurement",
        "calibration_cohort_id": COHORT,
    }
    row["component_observation_id"] = component_observation_id(row)
    return row


def _atomic_snapshot() -> dict[str, object]:
    components = [
        _component_row(ticker=ticker, ticker_index=ticker_index, spec_index=spec_index)
        for ticker_index, ticker in enumerate(("AAA", "BBB"))
        for spec_index in range(len(CORE_COMPONENT_SPECS))
    ]
    inputs: list[dict[str, object]] = []
    for ticker in ("AAA", "BBB"):
        observation_ids = sorted(
            str(row["component_observation_id"])
            for row in components
            if row["ticker"] == ticker
        )
        row: dict[str, object] = {
            "ticker": ticker,
            "asof_date": ASOF,
            "calibration_cohort_id": COHORT,
            "rank_ready_flag": 1,
            "review_reason": None,
            "source_id": STAGE6_SOURCE,
            "feature_status": "rank_ready",
            "calibration_eligible_flag": 1,
            "core_available_component_count": len(CORE_COMPONENT_SPECS),
            "core_missing_component_count": 0,
            "core_data_quality_confidence": 1.0,
            "full_data_quality_confidence": 1.0,
            "definition_version": "consumer_stage6_contract_test_v1",
            "contract_sha256": STAGE6_HASH,
            "lineage_json": json.dumps(
                {"component_observation_ids": observation_ids},
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        row["input_observation_id"] = input_observation_id(row)
        inputs.append(row)
    return {
        "schema_version": lineage.FEATURE_SNAPSHOT_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "input_rows": inputs,
        "input_rows_sha256": canonical_sha256(inputs),
        "component_rows": components,
        "component_rows_sha256": canonical_sha256(components),
    }


def _availability_artifacts(
    tmp_path: Path,
    atomic_payload: dict[str, object],
) -> tuple[Path, Path, str]:
    component_rows = atomic_payload["component_rows"]
    rows: list[dict[str, object]] = []
    for component in component_rows:
        component_id = str(component["component_observation_id"])
        rows.append(
            {
                "asof_date": ASOF,
                "ticker": component["ticker"],
                "component_name": component["component_name"],
                "component_observation_id": component_id,
                "availability_status": component["availability_status"],
                "source_required_flag": 1,
                "source_table": component["source_table"],
                "source_id": component["source_id"],
                "source_field": component["source_field"],
                "source_asof_date": component["source_asof_date"],
                "component_input_value_sha256": (
                    lineage.component_input_value_sha256(component)
                ),
                "source_available_at_utc": "2026-09-30T19:59:00+00:00",
                "source_observation_id": f"source-{component_id}",
                "source_locator": f"provider://{component_id}",
                "source_record_sha256": hashlib.sha256(
                    f"record-{component_id}".encode()
                ).hexdigest(),
                "provider_id": "provider",
                "dataset_id": "consumer-score-inputs",
            }
        )
    snapshot = {
        "schema_version": SCORE_INPUT_AVAILABILITY_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "snapshot_generated_at_utc": "2026-09-30T20:00:30+00:00",
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }
    snapshot_path = _write_json(tmp_path / "availability.json", snapshot)
    snapshot_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    source_observation_ids = sorted(
        str(row["source_observation_id"]) for row in rows
    )
    component_ids = sorted(
        str(row["component_observation_id"]) for row in rows
    )
    attestation = {
        "schema_version": SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
        "authority_id": "market",
        "signature_base64": "signature",
        "signed_payload_sha256": "c" * 64,
        "family": "consumer_defensive",
        "policy_id": POLICY_ID,
        "asof_date": ASOF,
        "availability_snapshot_sha256": snapshot_sha,
        "availability_rows_sha256": snapshot["rows_sha256"],
        "component_count": len(rows),
        "component_observation_ids_sha256": canonical_sha256(component_ids),
        "source_required_count": len(rows),
        "source_observation_ids_sha256": canonical_sha256(
            source_observation_ids
        ),
        "provider_id": "provider",
        "dataset_id": "consumer-score-inputs",
        "source_max_information_at_utc": "2026-09-30T19:59:00+00:00",
        "status_effective_through_at_utc": SIGNAL_CUTOFF,
        "exported_at_utc": "2026-09-30T20:01:00+00:00",
        "status_asof_policy": SCORE_INPUT_AVAILABILITY_POLICY,
        "query_sha256": "d" * 64,
    }
    attestation_path = _write_json(
        tmp_path / "availability-attestation.json",
        attestation,
    )
    return (
        snapshot_path,
        attestation_path,
        hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
    )


def _case(tmp_path: Path) -> tuple[Path, Path, Path]:
    baseline_payload = _baseline()
    atomic_payload = _atomic_snapshot()
    baseline_path = _write_json(tmp_path / "baseline.json", baseline_payload)
    atomic_path = _write_json(tmp_path / "atomic.json", atomic_payload)
    _availability_artifacts(tmp_path, atomic_payload)
    baseline_audit = lineage.validate_frozen_baseline_spec(
        baseline_path,
        expected_cohorts=[COHORT],
    )
    inputs, components = lineage._atomic_rows(  # noqa: SLF001
        atomic_payload,
        baseline=baseline_audit,
        asof=ASOF,
    )
    stage7 = lineage._replay_stage7(  # noqa: SLF001
        inputs,
        components,
        baseline=baseline_audit,
        asof=ASOF,
    )
    ordered = sorted(
        ("AAA", "BBB"),
        key=lambda ticker: (-float(stage7[ticker]["final_score"]), ticker),
    )
    rank_rows: list[dict[str, object]] = []
    for rank, ticker in enumerate(ordered, 1):
        final_score = float(stage7[ticker]["final_score"])
        rank_rows.append(
            {
                "asof_date": ASOF,
                "ticker": ticker,
                "sleeve_id": COHORT,
                "group_id": COHORT,
                "score": final_score,
                "rank": rank,
                "ranking_mode": "ranked",
                "eligible_flag": 1,
                "model_data_eligible_flag": 1,
                "lifecycle_eligible_flag": 1,
                "model_data_exclusion_reason_codes": [],
                "eligibility_exclusion_reason_codes": [],
                "selected_top_flag": int(rank == 1),
                "selected_bottom_flag": int(rank == len(ordered)),
                "baseline_candidate_id": "beverages_frozen_candidate",
                "frozen_model_identity_sha256": baseline_audit[
                    "model_identity_sha256"
                ],
                "stage6_input_observation_id": inputs[ticker][
                    "input_observation_id"
                ],
                "stage7_score_observation_id": stage7[ticker][
                    "score_observation_id"
                ],
            }
        )
    rank_payload = {
        "schema_version": RANK_SNAPSHOT_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "rows": rank_rows,
        "rows_sha256": canonical_sha256(rank_rows),
    }
    rank_path = _write_json(tmp_path / "rank.json", rank_payload)
    return baseline_path, atomic_path, rank_path


def _replay(
    *,
    tmp_path: Path,
    baseline: Path,
    atomic: Path,
    rank: Path,
) -> dict[str, object]:
    attestation_path = tmp_path / "availability-attestation.json"
    return lineage.validate_and_replay_consumer_scores(
        asof_date=ASOF,
        signal_cutoff_at_utc=SIGNAL_CUTOFF,
        rank_snapshot_path=rank,
        feature_snapshot_path=atomic,
        frozen_baseline_spec_path=baseline,
        score_input_availability_snapshot_path=tmp_path / "availability.json",
        score_input_availability_attestation_path=attestation_path,
        expected_score_input_availability_attestation_sha256=hashlib.sha256(
            attestation_path.read_bytes()
        ).hexdigest(),
        canonical_trust_bundle=_bundle(),
        policy_id=POLICY_ID,
        expected_cohorts=[COHORT],
    )


def _resign_availability(
    tmp_path: Path,
    *,
    max_information_at_utc: str,
) -> None:
    snapshot_path = tmp_path / "availability.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["rows_sha256"] = canonical_sha256(snapshot["rows"])
    _write_json(snapshot_path, snapshot)
    attestation_path = tmp_path / "availability-attestation.json"
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    attestation["availability_snapshot_sha256"] = hashlib.sha256(
        snapshot_path.read_bytes()
    ).hexdigest()
    attestation["availability_rows_sha256"] = snapshot["rows_sha256"]
    attestation["source_max_information_at_utc"] = max_information_at_utc
    _write_json(attestation_path, attestation)


def test_consumer_atomic_score_replay_accepts_exact_frozen_model(
    tmp_path: Path,
) -> None:
    baseline, atomic, rank = _case(tmp_path)
    audit = _replay(
        tmp_path=tmp_path,
        baseline=baseline,
        atomic=atomic,
        rank=rank,
    )
    assert audit["exact_model_score_replay_pass"] is True
    assert audit["no_reestimation_from_outcomes_pass"] is True
    assert audit["ticker_count"] == 2
    assert all(
        row["model_data_eligible_flag"] == 1
        for row in audit["model_data_eligibility_by_ticker"].values()
    )
    assert audit["exact_source_availability_crosswalk_pass"] is True
    assert audit["max_source_available_at_utc"].startswith(
        "2026-09-30T19:59:00"
    )


def test_public_atomic_snapshot_validator_proves_exact_stage6_census(
    tmp_path: Path,
) -> None:
    baseline, atomic, _ = _case(tmp_path)

    audit = lineage.validate_consumer_atomic_feature_snapshot(
        asof_date=ASOF,
        feature_snapshot_path=atomic,
        frozen_baseline_spec_path=baseline,
        expected_cohorts=[COHORT],
    )

    assert audit["ticker_count"] == 2
    assert audit["exact_stage6_identity_replay_pass"] is True
    assert audit["no_post_asof_component_sources_pass"] is True


def test_public_atomic_snapshot_validator_rejects_post_asof_source(
    tmp_path: Path,
) -> None:
    baseline, atomic, _ = _case(tmp_path)
    payload = json.loads(atomic.read_text(encoding="utf-8"))
    payload["component_rows"][0]["source_asof_date"] = "2026-10-01"
    payload["component_rows_sha256"] = canonical_sha256(
        payload["component_rows"]
    )
    _write_json(atomic, payload)

    with pytest.raises(ValueError, match="source is post-checkpoint"):
        lineage.validate_consumer_atomic_feature_snapshot(
            asof_date=ASOF,
            feature_snapshot_path=atomic,
            frozen_baseline_spec_path=baseline,
            expected_cohorts=[COHORT],
        )


def test_public_atomic_snapshot_validator_rejects_extra_top_level_fields(
    tmp_path: Path,
) -> None:
    baseline, atomic, _ = _case(tmp_path)
    payload = json.loads(atomic.read_text(encoding="utf-8"))
    payload["future_return"] = 1.0
    _write_json(atomic, payload)

    with pytest.raises(ValueError, match="top-level census changed"):
        lineage.validate_consumer_atomic_feature_snapshot(
            asof_date=ASOF,
            feature_snapshot_path=atomic,
            frozen_baseline_spec_path=baseline,
            expected_cohorts=[COHORT],
        )


def test_multi_failure_model_data_reason_codes_are_canonical() -> None:
    eligible, reasons = lineage._model_data_eligibility(  # noqa: SLF001
        stage7_eligible_flag=0,
        available_weight=0.50,
        missing_weight=0.50,
        minimum_quality=0.80,
        maximum_missing=0.20,
    )

    assert eligible == 0
    assert reasons == [
        "candidate_low_data_quality",
        "candidate_missing_weight_exceeded",
        "stage7_model_data_not_rank_ready",
    ]


@pytest.mark.parametrize("value", [True, "2", 2.0])
def test_frozen_integer_contract_rejects_bool_string_and_fractional_types(
    tmp_path: Path,
    value: object,
) -> None:
    payload = _baseline()
    payload["minimum_normalization_peer_count"] = value
    path = _write_json(tmp_path / "baseline.json", payload)

    with pytest.raises(ValueError, match="canonical integer"):
        lineage.validate_frozen_baseline_spec(path, expected_cohorts=[COHORT])


@pytest.mark.parametrize("value", [True, "50"])
def test_frozen_numeric_contract_rejects_bool_and_numeric_strings(
    tmp_path: Path,
    value: object,
) -> None:
    payload = _baseline()
    payload["neutral_score"] = value
    path = _write_json(tmp_path / "baseline.json", payload)

    with pytest.raises(ValueError, match="must be numeric"):
        lineage.validate_frozen_baseline_spec(path, expected_cohorts=[COHORT])


def test_coherent_score_and_rank_rewrite_cannot_bypass_frozen_replay(
    tmp_path: Path,
) -> None:
    baseline, atomic, rank = _case(tmp_path)
    payload = json.loads(rank.read_text(encoding="utf-8"))
    rows = copy.deepcopy(payload["rows"])
    rows.reverse()
    for index, row in enumerate(rows, 1):
        row["score"] = 100.0 - float(row["score"])
        row["rank"] = index
        row["selected_top_flag"] = int(index == 1)
        row["selected_bottom_flag"] = int(index == len(rows))
    payload["rows"] = rows
    payload["rows_sha256"] = canonical_sha256(rows)
    _write_json(rank, payload)
    with pytest.raises(ValueError, match="rank score differs"):
        _replay(
            tmp_path=tmp_path,
            baseline=baseline,
            atomic=atomic,
            rank=rank,
        )


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_rank_json_rejects_noncanonical_integer_types(
    tmp_path: Path,
    value: object,
) -> None:
    baseline, atomic, rank = _case(tmp_path)
    payload = json.loads(rank.read_text(encoding="utf-8"))
    payload["rows"][0]["rank"] = value
    payload["rows_sha256"] = canonical_sha256(payload["rows"])
    _write_json(rank, payload)

    with pytest.raises(ValueError, match="canonical integer"):
        _replay(
            tmp_path=tmp_path,
            baseline=baseline,
            atomic=atomic,
            rank=rank,
        )


def test_consumer_replay_rejects_same_day_post_cutoff_component_source(
    tmp_path: Path,
) -> None:
    baseline, atomic, rank = _case(tmp_path)
    availability_path = tmp_path / "availability.json"
    availability = json.loads(availability_path.read_text(encoding="utf-8"))
    availability["rows"][0]["source_available_at_utc"] = (
        "2026-09-30T20:00:00.001Z"
    )
    _write_json(availability_path, availability)
    _resign_availability(
        tmp_path,
        max_information_at_utc="2026-09-30T20:00:00.001Z",
    )

    with pytest.raises(ValueError, match="available after cutoff"):
        _replay(
            tmp_path=tmp_path,
            baseline=baseline,
            atomic=atomic,
            rank=rank,
        )


def test_consumer_replay_rejects_attested_component_value_mismatch(
    tmp_path: Path,
) -> None:
    baseline, atomic, rank = _case(tmp_path)
    availability_path = tmp_path / "availability.json"
    availability = json.loads(availability_path.read_text(encoding="utf-8"))
    availability["rows"][0]["component_input_value_sha256"] = "f" * 64
    _write_json(availability_path, availability)
    _resign_availability(
        tmp_path,
        max_information_at_utc="2026-09-30T19:59:00+00:00",
    )

    with pytest.raises(
        ValueError,
        match="attested source availability differs from atomic component",
    ):
        _replay(
            tmp_path=tmp_path,
            baseline=baseline,
            atomic=atomic,
            rank=rank,
        )


@pytest.mark.parametrize(
    ("artifact", "ticker"),
    [
        ("input", "aaa"),
        ("component", "AAA "),
        ("rank", " aaa"),
    ],
)
def test_consumer_atomic_and_rank_tickers_require_exact_uppercase(
    tmp_path: Path,
    artifact: str,
    ticker: str,
) -> None:
    baseline, atomic, rank = _case(tmp_path)
    if artifact in {"input", "component"}:
        payload = json.loads(atomic.read_text(encoding="utf-8"))
        field = "input_rows" if artifact == "input" else "component_rows"
        payload[field][0]["ticker"] = ticker
        payload[f"{field}_sha256"] = canonical_sha256(payload[field])
        _write_json(atomic, payload)
    else:
        payload = json.loads(rank.read_text(encoding="utf-8"))
        payload["rows"][0]["ticker"] = ticker
        payload["rows_sha256"] = canonical_sha256(payload["rows"])
        _write_json(rank, payload)

    with pytest.raises(ValueError, match="canonical uppercase"):
        _replay(
            tmp_path=tmp_path,
            baseline=baseline,
            atomic=atomic,
            rank=rank,
        )


def test_frozen_baseline_snapshot_hash_is_from_parsed_bytes(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "baseline.json", _baseline())
    audit = lineage.validate_frozen_baseline_spec(
        path,
        expected_cohorts=[COHORT],
    )
    snapshot = audit["source_snapshot"]
    assert snapshot["path"] == str(path.resolve())
    assert snapshot["bytes"] == len(path.read_bytes())
    assert snapshot["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(str(audit["model_identity_sha256"])) == 64


def test_nonzero_specialized_weight_cannot_enter_unvalidated_replay(
    tmp_path: Path,
) -> None:
    payload = _baseline()
    payload["specialized_component_names"] = ["specialized:unvalidated_metric"]
    model = payload["cohort_models"][COHORT]
    model["core_weights"] = _weights(0.90)
    model["specialized_weights"] = {"specialized:unvalidated_metric": 0.10}
    path = _write_json(tmp_path / "baseline.json", payload)
    with pytest.raises(ValueError, match="nonzero specialized weight"):
        lineage.validate_frozen_baseline_spec(
            path,
            expected_cohorts=[COHORT],
        )


def test_weight_fixture_is_numerically_exact_enough_for_contract() -> None:
    assert math.isclose(sum(_weights().values()), 1.0, abs_tol=1e-12)

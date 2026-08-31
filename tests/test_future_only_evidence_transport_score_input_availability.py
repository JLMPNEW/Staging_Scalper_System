from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.transport_score_input_availability import (
    ACTIVATION_BASELINE_ROLE,
    FACT_INPUT_KIND,
    PANEL_INPUT_KIND,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
    TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
    transport_fact_identity,
    transport_fact_input_value_sha256,
    transport_panel_input_identity,
    transport_panel_input_value_sha256,
    validate_transport_score_input_availability_capture_chronology,
    validate_transport_score_input_availability_snapshot,
)


ASOF = "2026-08-26"
CUTOFF = "2026-08-26T21:00:00+00:00"
POLICY = "transportation_v8_subgroup_future_oos_v6"


class _Authority:
    def __init__(self) -> None:
        self.calls = 0

    def verify_snapshot(
        self,
        payload_bytes: bytes,
        digest: str,
        payload: dict[str, object],
    ) -> bool:
        assert hashlib.sha256(payload_bytes).hexdigest() == digest
        assert json.loads(payload_bytes.decode("utf-8")) == payload
        self.calls += 1
        return True


def _bundle(authority: _Authority) -> SimpleNamespace:
    return SimpleNamespace(
        market_data_export=authority,
        allowed_provider_ids=frozenset({"provider"}),
        allowed_dataset_ids=frozenset({"transport-score-inputs"}),
    )


def _panel_rows() -> list[dict[str, object]]:
    return [
        {
            "asof_date": score_date,
            "ticker": ticker,
            "horizon_sessions": 63,
            "calibration_cohort": "surface_freight",
            "metric_values_json": json.dumps(
                {"gross_margin": value}, sort_keys=True, separators=(",", ":")
            ),
            "metric_status_json": json.dumps(
                {"gross_margin": "available"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "positioning_score": 55.0 + value,
            "rank_ready_flag": 1,
            "calibration_eligible_flag": 1,
            "source_score_sha256": hashlib.sha256(
                f"score|{score_date}".encode()
            ).hexdigest(),
        }
        for score_date, ticker, value in (
            ("2025-12-31", "AAA", 1.0),
            (ASOF, "BBB", 2.0),
        )
    ]


def _fact_rows() -> list[dict[str, object]]:
    return [
        {
            "ticker": "AAA",
            "metric_id": "case_volume",
            "value": 10.0,
            "unit": "cases",
            "period_end": "2026-06-30",
            "filing_date": "2026-08-01",
            "accepted_at": "2026-08-20T15:00:00Z",
            "reviewed_at": "2026-08-21T15:00:00Z",
            "replay_status": "ACCEPTED",
            "candidate_key": "AAA|case_volume|2026-06-30",
            "evidence_key": None,
            "source_content_sha256": "a" * 64,
        }
    ]


def _availability_row(
    source: dict[str, object],
    *,
    kind: str,
    observation_id: str,
    available_at: str,
) -> dict[str, object]:
    if kind == PANEL_INPUT_KIND:
        identity = transport_panel_input_identity(source)
        value_sha = transport_panel_input_value_sha256(source)
    else:
        identity = transport_fact_identity(source)
        value_sha = transport_fact_input_value_sha256(source)
    return {
        "input_kind": kind,
        "ticker": source["ticker"],
        "input_identity_sha256": identity,
        "input_content_sha256": canonical_sha256(source),
        "input_value_sha256": value_sha,
        "availability_status": "available",
        "source_required_flag": 1,
        "source_id": "governed-export",
        "source_available_at_utc": available_at,
        "source_observation_id": observation_id,
        "source_locator": f"provider://{source['ticker']}/{observation_id}",
        "source_record_sha256": hashlib.sha256(observation_id.encode()).hexdigest(),
        "provider_id": "provider",
        "dataset_id": "transport-score-inputs",
    }


def _mapping(row: dict[str, object]) -> dict[str, object]:
    return {
        "input_kind": row["input_kind"],
        "ticker": row["ticker"],
        "input_identity_sha256": row["input_identity_sha256"],
        "input_content_sha256": row["input_content_sha256"],
        "input_value_sha256": row["input_value_sha256"],
        "source_observation_id": row["source_observation_id"],
        "source_record_sha256": row["source_record_sha256"],
        "source_available_at_utc": str(row["source_available_at_utc"]).replace(
            "Z", "+00:00"
        ),
        "source_locator": row["source_locator"],
        "source_id": row["source_id"],
        "provider_id": row["provider_id"],
        "dataset_id": row["dataset_id"],
    }


def _artifacts(
    tmp_path: Path,
    *,
    panel: list[dict[str, object]] | None = None,
    facts: list[dict[str, object]] | None = None,
    panel_availability: list[dict[str, object]] | None = None,
    fact_availability: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], bytes, bytes, list[dict[str, object]], list[dict[str, object]]]:
    panel = deepcopy(_panel_rows() if panel is None else panel)
    facts = deepcopy(_fact_rows() if facts is None else facts)
    if panel_availability is None:
        panel_availability = [
            _availability_row(
                row,
                kind=PANEL_INPUT_KIND,
                observation_id=f"panel-{index}",
                available_at=(
                    "2025-12-31T21:00:00Z"
                    if index == 0
                    else "2026-08-26T20:59:00Z"
                ),
            )
            for index, row in enumerate(panel)
        ]
    if fact_availability is None:
        fact_availability = [
            _availability_row(
                row,
                kind=FACT_INPUT_KIND,
                observation_id=f"fact-{index}",
                available_at="2026-08-21T15:00:00Z",
            )
            for index, row in enumerate(facts)
        ]
    snapshot = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "snapshot_generated_at_utc": "2026-08-26T21:00:30Z",
        "panel_input_rows": panel_availability,
        "panel_input_rows_sha256": canonical_sha256(panel_availability),
        "accepted_fact_input_rows": fact_availability,
        "accepted_fact_input_rows_sha256": canonical_sha256(fact_availability),
    }
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    snapshot_path = tmp_path / "availability.json"
    snapshot_path.write_bytes(snapshot_bytes)
    identity_census = {
        "panel": [transport_panel_input_identity(row) for row in panel],
        "accepted_facts": [transport_fact_identity(row) for row in facts],
    }
    content_census = {
        "panel": [canonical_sha256(row) for row in panel],
        "accepted_facts": [canonical_sha256(row) for row in facts],
    }
    value_census = {
        "panel": [transport_panel_input_value_sha256(row) for row in panel],
        "accepted_facts": [transport_fact_input_value_sha256(row) for row in facts],
    }
    mappings = [_mapping(row) for row in panel_availability + fact_availability]
    pairs = [{"provider_id": "provider", "dataset_id": "transport-score-inputs"}]
    max_available = max(
        str(row["source_available_at_utc"]).replace("Z", "+00:00")
        for row in panel_availability + fact_availability
    )
    attestation = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
        "authority_id": "market",
        "signature_base64": "signature",
        "signed_payload_sha256": "b" * 64,
        "family": "transportation",
        "policy_id": POLICY,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "availability_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "panel_input_rows_sha256": snapshot["panel_input_rows_sha256"],
        "accepted_fact_input_rows_sha256": snapshot[
            "accepted_fact_input_rows_sha256"
        ],
        "input_count": len(panel) + len(facts),
        "panel_input_count": len(panel),
        "accepted_fact_input_count": len(facts),
        "input_identity_census_sha256": canonical_sha256(identity_census),
        "input_content_census_sha256": canonical_sha256(content_census),
        "input_value_census_sha256": canonical_sha256(value_census),
        "source_observation_mapping_sha256": canonical_sha256(mappings),
        "provider_dataset_pair_count": 1,
        "provider_dataset_pairs_sha256": canonical_sha256(pairs),
        "source_max_information_at_utc": max_available,
        "status_effective_through_at_utc": CUTOFF,
        "exported_at_utc": "2026-08-26T21:01:00+00:00",
        "status_asof_policy": TRANSPORT_SCORE_INPUT_AVAILABILITY_POLICY,
        "query_sha256": "c" * 64,
    }
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_bytes(attestation_bytes)
    kwargs: dict[str, object] = {
        "path": snapshot_path,
        "asof_date": ASOF,
        "expected_panel_rows": panel,
        "expected_accepted_fact_rows": facts,
        "signal_cutoff_at_utc": CUTOFF,
        "policy_id": POLICY,
        "attestation_path": attestation_path,
        "expected_attestation_sha256": hashlib.sha256(attestation_bytes).hexdigest(),
    }
    return kwargs, snapshot_bytes, attestation_bytes, panel_availability, fact_availability


def test_transport_score_inputs_use_exact_signed_bytes_and_capture_envelope(
    tmp_path: Path,
) -> None:
    authority = _Authority()
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(tmp_path)
    Path(kwargs["path"]).write_text("changed", encoding="utf-8")
    Path(kwargs["attestation_path"]).write_text("changed", encoding="utf-8")

    panel, facts, audit = validate_transport_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(authority),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )
    timing = {
        "signal_information_cutoff_at_utc": CUTOFF,
        "source_max_information_at_utc": "2026-08-26T20:59:00+00:00",
        "source_generated_at_utc": "2026-08-26T21:01:30+00:00",
        "captured_at_utc": "2026-08-26T21:02:00+00:00",
        "entry_execution_at_utc": "2026-08-27T13:30:00+00:00",
    }
    envelope = validate_transport_score_input_availability_capture_chronology(
        audit,
        trusted_capture_timing=timing,
        captured_at_utc=timing["captured_at_utc"],
        label="Transportation test",
    )

    assert len(panel) == 2
    assert len(facts) == 1
    assert audit["exact_input_content_and_value_pass"] is True
    assert envelope["availability_within_signed_source_envelope_pass"] is True
    assert authority.calls == 1


def test_transport_activation_baseline_role_is_explicitly_signed(
    tmp_path: Path,
) -> None:
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(tmp_path)
    snapshot = json.loads(snapshot_bytes)
    snapshot["evidence_role"] = ACTIVATION_BASELINE_ROLE
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    attestation = json.loads(attestation_bytes)
    attestation["evidence_role"] = ACTIVATION_BASELINE_ROLE
    attestation["availability_snapshot_sha256"] = hashlib.sha256(
        snapshot_bytes
    ).hexdigest()
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    kwargs["expected_attestation_sha256"] = hashlib.sha256(
        attestation_bytes
    ).hexdigest()
    kwargs["expected_evidence_role"] = ACTIVATION_BASELINE_ROLE

    _, _, audit = validate_transport_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(_Authority()),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )

    assert audit["evidence_role"] == ACTIVATION_BASELINE_ROLE


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("missing_panel", "census is not exact"),
        ("reordered_panel", "differs from input"),
        ("extra_fact", "census is not exact"),
        ("uppercase_hash", "exact lowercase SHA-256"),
        ("unallowlisted_provider", "outside trust allowlists"),
    ],
)
def test_transport_score_input_census_and_signed_syntax_are_exact(
    tmp_path: Path,
    case: str,
    match: str,
) -> None:
    panel = _panel_rows()
    facts = _fact_rows()
    panel_availability = [
        _availability_row(
            row,
            kind=PANEL_INPUT_KIND,
            observation_id=f"panel-{index}",
            available_at="2026-08-26T20:59:00Z",
        )
        for index, row in enumerate(panel)
    ]
    fact_availability = [
        _availability_row(
            facts[0],
            kind=FACT_INPUT_KIND,
            observation_id="fact-0",
            available_at="2026-08-21T15:00:00Z",
        )
    ]
    if case == "missing_panel":
        panel_availability.pop()
    elif case == "reordered_panel":
        panel_availability.reverse()
    elif case == "extra_fact":
        fact_availability.append(deepcopy(fact_availability[0]))
    elif case == "uppercase_hash":
        panel_availability[0]["input_identity_sha256"] = str(
            panel_availability[0]["input_identity_sha256"]
        ).upper()
    else:
        panel_availability[0]["provider_id"] = "invented-provider"
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(
        tmp_path,
        panel=panel,
        facts=facts,
        panel_availability=panel_availability,
        fact_availability=fact_availability,
    )

    with pytest.raises(ValueError, match=match):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


@pytest.mark.parametrize("field", ["metric_values_json", "positioning_score"])
def test_coherent_panel_value_rewrite_lacks_matching_attested_content(
    tmp_path: Path,
    field: str,
) -> None:
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(tmp_path)
    panel = deepcopy(kwargs["expected_panel_rows"])
    panel[1][field] = 99.0 if field == "positioning_score" else '{"gross_margin":99.0}'
    kwargs["expected_panel_rows"] = panel

    with pytest.raises(ValueError, match="differs from input"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_coherent_fact_value_rewrite_lacks_matching_attested_content(
    tmp_path: Path,
) -> None:
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(tmp_path)
    facts = deepcopy(kwargs["expected_accepted_fact_rows"])
    facts[0]["value"] = 999.0
    kwargs["expected_accepted_fact_rows"] = facts

    with pytest.raises(ValueError, match="differs from input"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_post_close_source_cannot_pass_with_backdated_fact_acceptance(
    tmp_path: Path,
) -> None:
    facts = _fact_rows()
    facts[0]["accepted_at"] = "2026-08-26T20:59:00Z"
    fact_availability = [
        _availability_row(
            facts[0],
            kind=FACT_INPUT_KIND,
            observation_id="post-close-fact",
            available_at="2026-08-26T21:00:00.001Z",
        )
    ]
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(
        tmp_path, facts=facts, fact_availability=fact_availability
    )

    with pytest.raises(ValueError, match="available after cutoff"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_shared_source_record_reuse_requires_identical_provenance(
    tmp_path: Path,
) -> None:
    panel = _panel_rows()
    first = _availability_row(
        panel[0],
        kind=PANEL_INPUT_KIND,
        observation_id="shared",
        available_at="2026-08-26T20:59:00Z",
    )
    second = _availability_row(
        panel[1],
        kind=PANEL_INPUT_KIND,
        observation_id="shared",
        available_at="2026-08-26T20:59:00Z",
    )
    second["source_record_sha256"] = "d" * 64
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(
        tmp_path, panel=panel, panel_availability=[first, second]
    )

    with pytest.raises(ValueError, match="inconsistent provenance"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_predecessor_availability_prefix_cannot_be_rewritten(
    tmp_path: Path,
) -> None:
    kwargs, _, _, panel_availability, _ = _artifacts(tmp_path)
    predecessor = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "source_attestation_schema": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "evidence_role": ACTIVATION_BASELINE_ROLE,
        "asof_date": "2026-07-31",
        "signal_cutoff_at_utc": "2026-07-31T20:00:00+00:00",
        "panel_input_row_count": 1,
        "accepted_fact_input_row_count": 0,
        "full_panel_input_rows_sha256": canonical_sha256(panel_availability[:1]),
        "full_accepted_fact_input_rows_sha256": canonical_sha256([]),
        "exact_panel_input_census_pass": True,
        "exact_accepted_fact_input_census_pass": True,
        "no_post_cutoff_score_inputs_pass": True,
        "no_backdated_panel_append_pass": True,
        "no_backdated_fact_append_pass": True,
    }
    mutated_panel_availability = deepcopy(panel_availability)
    mutated_panel_availability[0]["source_locator"] = "provider://AAA/rewritten"
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(
        tmp_path,
        panel_availability=mutated_panel_availability,
    )
    kwargs["predecessor_availability_audit"] = predecessor

    with pytest.raises(ValueError, match="predecessor prefix changed"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_new_panel_input_cannot_be_backdated_before_predecessor_cutoff(
    tmp_path: Path,
) -> None:
    kwargs, _, _, panel_availability, _ = _artifacts(tmp_path)
    predecessor = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "source_attestation_schema": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "evidence_role": ACTIVATION_BASELINE_ROLE,
        "asof_date": "2026-07-31",
        "signal_cutoff_at_utc": "2026-07-31T20:00:00+00:00",
        "panel_input_row_count": 1,
        "accepted_fact_input_row_count": 0,
        "full_panel_input_rows_sha256": canonical_sha256(panel_availability[:1]),
        "full_accepted_fact_input_rows_sha256": canonical_sha256([]),
        "exact_panel_input_census_pass": True,
        "exact_accepted_fact_input_census_pass": True,
        "no_post_cutoff_score_inputs_pass": True,
        "no_backdated_panel_append_pass": True,
        "no_backdated_fact_append_pass": True,
    }
    backdated_panel_availability = deepcopy(panel_availability)
    backdated_panel_availability[1]["source_available_at_utc"] = (
        "2026-07-31T20:00:00Z"
    )
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(
        tmp_path,
        panel_availability=backdated_panel_availability,
    )
    kwargs["predecessor_availability_audit"] = predecessor

    with pytest.raises(ValueError, match="backdated at/before baseline cutoff"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_new_fact_input_cannot_be_backdated_before_predecessor_cutoff(
    tmp_path: Path,
) -> None:
    kwargs, _, _, panel_availability, fact_availability = _artifacts(tmp_path)
    predecessor = {
        "schema_version": TRANSPORT_SCORE_INPUT_AVAILABILITY_SCHEMA,
        "source_attestation_schema": (
            TRANSPORT_SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA
        ),
        "evidence_role": ACTIVATION_BASELINE_ROLE,
        "asof_date": "2026-07-31",
        "signal_cutoff_at_utc": "2026-07-31T20:00:00+00:00",
        "panel_input_row_count": 1,
        "accepted_fact_input_row_count": 0,
        "full_panel_input_rows_sha256": canonical_sha256(panel_availability[:1]),
        "full_accepted_fact_input_rows_sha256": canonical_sha256([]),
        "exact_panel_input_census_pass": True,
        "exact_accepted_fact_input_census_pass": True,
        "no_post_cutoff_score_inputs_pass": True,
        "no_backdated_panel_append_pass": True,
        "no_backdated_fact_append_pass": True,
    }
    backdated_fact_availability = deepcopy(fact_availability)
    backdated_fact_availability[0]["source_available_at_utc"] = (
        "2026-07-31T20:00:00Z"
    )
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(
        tmp_path,
        fact_availability=backdated_fact_availability,
    )
    kwargs["predecessor_availability_audit"] = predecessor

    with pytest.raises(ValueError, match="backdated at/before baseline cutoff"):
        validate_transport_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_signed_capture_envelope_rejects_late_export(tmp_path: Path) -> None:
    kwargs, snapshot_bytes, attestation_bytes, _, _ = _artifacts(tmp_path)
    _, _, audit = validate_transport_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(_Authority()),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )
    timing = {
        "signal_information_cutoff_at_utc": CUTOFF,
        "source_max_information_at_utc": "2026-08-26T20:59:00+00:00",
        "source_generated_at_utc": "2026-08-26T21:00:59+00:00",
        "captured_at_utc": "2026-08-26T21:02:00+00:00",
        "entry_execution_at_utc": "2026-08-27T13:30:00+00:00",
    }

    with pytest.raises(ValueError, match="exceeds signed capture timing"):
        validate_transport_score_input_availability_capture_chronology(
            audit,
            trusted_capture_timing=timing,
            captured_at_utc=timing["captured_at_utc"],
            label="Transportation test",
        )

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.score_input_availability import (
    SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
    SCORE_INPUT_AVAILABILITY_POLICY,
    SCORE_INPUT_AVAILABILITY_SCHEMA,
    validate_score_input_availability_capture_chronology,
    validate_score_input_availability_snapshot,
)


ASOF = "2026-09-30"
CUTOFF = "2026-09-30T20:00:00+00:00"
COMPONENT_IDS = ["0" * 64, "1" * 64]


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
        allowed_dataset_ids=frozenset({"score-inputs"}),
    )


def _rows() -> list[dict[str, object]]:
    common = {
        "asof_date": ASOF,
        "provider_id": "provider",
        "dataset_id": "score-inputs",
    }
    return [
        {
            **common,
            "ticker": "AAA",
            "component_name": "quality",
            "component_observation_id": COMPONENT_IDS[0],
            "availability_status": "available",
            "source_required_flag": 1,
            "source_table": "feature_financial_statement",
            "source_id": "sec_companyfacts",
            "source_field": "gross_margin",
            "source_asof_date": ASOF,
            "component_input_value_sha256": "2" * 64,
            "source_available_at_utc": "2026-09-30T19:59:00+00:00",
            "source_observation_id": "source-AAA-quality",
            "source_locator": "provider://AAA/quality",
            "source_record_sha256": "3" * 64,
        },
        {
            **common,
            "ticker": "BBB",
            "component_name": "specialized:metric",
            "component_observation_id": COMPONENT_IDS[1],
            "availability_status": "not_loaded",
            "source_required_flag": 0,
            "source_table": "fact_specialized_metric_observation",
            "source_id": None,
            "source_field": "metric",
            "source_asof_date": None,
            "component_input_value_sha256": "4" * 64,
            "source_available_at_utc": None,
            "source_observation_id": None,
            "source_locator": None,
            "source_record_sha256": None,
        },
    ]


def _artifacts(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    max_information: str | None = "2026-09-30T19:59:00+00:00",
) -> tuple[dict[str, object], bytes, bytes]:
    snapshot = {
        "schema_version": SCORE_INPUT_AVAILABILITY_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "snapshot_generated_at_utc": "2026-09-30T20:00:30+00:00",
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    snapshot_path = tmp_path / "availability.json"
    snapshot_path.write_bytes(snapshot_bytes)
    source_ids = sorted(
        str(row["source_observation_id"])
        for row in rows
        if row["source_required_flag"] == 1
    )
    attestation = {
        "schema_version": SCORE_INPUT_AVAILABILITY_ATTESTATION_SCHEMA,
        "authority_id": "market",
        "signature_base64": "signature",
        "signed_payload_sha256": "5" * 64,
        "family": "consumer_defensive",
        "policy_id": "test-policy",
        "asof_date": ASOF,
        "availability_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "availability_rows_sha256": snapshot["rows_sha256"],
        "component_count": len(COMPONENT_IDS),
        "component_observation_ids_sha256": canonical_sha256(
            sorted(COMPONENT_IDS)
        ),
        "source_required_count": len(source_ids),
        "source_observation_ids_sha256": canonical_sha256(source_ids),
        "provider_id": "provider",
        "dataset_id": "score-inputs",
        "source_max_information_at_utc": max_information,
        "status_effective_through_at_utc": CUTOFF,
        "exported_at_utc": "2026-09-30T20:01:00+00:00",
        "status_asof_policy": SCORE_INPUT_AVAILABILITY_POLICY,
        "query_sha256": "6" * 64,
    }
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_bytes(attestation_bytes)
    kwargs: dict[str, object] = {
        "path": snapshot_path,
        "asof_date": ASOF,
        "expected_component_observation_ids": COMPONENT_IDS,
        "signal_cutoff_at_utc": CUTOFF,
        "family": "consumer_defensive",
        "policy_id": "test-policy",
        "attestation_path": attestation_path,
        "expected_attestation_sha256": hashlib.sha256(
            attestation_bytes
        ).hexdigest(),
    }
    return kwargs, snapshot_bytes, attestation_bytes


def test_score_input_availability_uses_exact_attested_bytes(
    tmp_path: Path,
) -> None:
    authority = _Authority()
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, _rows())
    Path(kwargs["path"]).write_text("changed", encoding="utf-8")
    Path(kwargs["attestation_path"]).write_text("changed", encoding="utf-8")

    index, audit = validate_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(authority),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )

    assert set(index) == set(COMPONENT_IDS)
    assert audit["exact_official_close_cutoff_pass"] is True
    assert audit["max_source_available_at_utc"].startswith(
        "2026-09-30T19:59:00"
    )
    assert authority.calls == 1


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("missing", "exact component observation census"),
        ("lowercase", "canonical uppercase"),
        ("post_cutoff", "available after cutoff"),
        ("required_null", "nonblank canonical string"),
        ("no_source_payload", "no-source availability row"),
    ],
)
def test_score_input_availability_rejects_census_and_source_mutations(
    tmp_path: Path,
    case: str,
    match: str,
) -> None:
    rows = deepcopy(_rows())
    if case == "missing":
        rows.pop()
    elif case == "lowercase":
        rows[0]["ticker"] = "aaa"
    elif case == "post_cutoff":
        rows[0]["source_available_at_utc"] = "2026-09-30T20:00:00.001Z"
    elif case == "required_null":
        rows[0]["source_observation_id"] = None
    else:
        rows[1]["source_observation_id"] = "invented"
    max_information = (
        "2026-09-30T20:00:00.001Z"
        if case == "post_cutoff"
        else "2026-09-30T19:59:00+00:00"
    )
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(
        tmp_path,
        rows,
        max_information=max_information,
    )

    with pytest.raises(ValueError, match=match):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_score_input_availability_rejects_wrong_attested_max(
    tmp_path: Path,
) -> None:
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(
        tmp_path,
        _rows(),
        max_information="2026-09-30T19:58:00+00:00",
    )

    with pytest.raises(ValueError, match="max information time differs"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_shared_source_observation_can_feed_multiple_components_when_record_is_exact(
    tmp_path: Path,
) -> None:
    rows = deepcopy(_rows())
    rows[1].update(
        {
            "ticker": "AAA",
            "component_name": "quality-secondary",
            "availability_status": "available",
            "source_required_flag": 1,
            "source_table": rows[0]["source_table"],
            "source_id": rows[0]["source_id"],
            "source_field": "operating_margin",
            "source_asof_date": rows[0]["source_asof_date"],
            "source_available_at_utc": rows[0]["source_available_at_utc"],
            "source_observation_id": rows[0]["source_observation_id"],
            "source_locator": rows[0]["source_locator"],
            "source_record_sha256": rows[0]["source_record_sha256"],
        }
    )
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)

    _, audit = validate_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(_Authority()),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )

    assert audit["source_required_count"] == 2
    assert audit["unique_source_observation_count"] == 1
    assert audit["consistent_shared_source_observation_identity_pass"] is True
    assert len(audit["component_source_mapping_sha256"]) == 64


def test_shared_source_observation_rejects_inconsistent_record_identity(
    tmp_path: Path,
) -> None:
    rows = deepcopy(_rows())
    rows[1].update(
        {
            "ticker": "AAA",
            "component_name": "quality-secondary",
            "availability_status": "available",
            "source_required_flag": 1,
            "source_table": rows[0]["source_table"],
            "source_id": rows[0]["source_id"],
            "source_field": "operating_margin",
            "source_asof_date": rows[0]["source_asof_date"],
            "source_available_at_utc": rows[0]["source_available_at_utc"],
            "source_observation_id": rows[0]["source_observation_id"],
            "source_locator": rows[0]["source_locator"],
            "source_record_sha256": "9" * 64,
        }
    )
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)

    with pytest.raises(ValueError, match="inconsistent record identity"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


@pytest.mark.parametrize(
    "timestamp",
    ["2026-09-30 19:59:00Z", "2026-09-30T19:59:00.1234567Z"],
)
def test_score_input_availability_requires_exact_rfc3339_utc(
    tmp_path: Path,
    timestamp: str,
) -> None:
    rows = deepcopy(_rows())
    rows[0]["source_available_at_utc"] = timestamp
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(
        tmp_path,
        rows,
        max_information=timestamp,
    )

    with pytest.raises(ValueError, match="exact RFC3339 UTC"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_score_input_availability_rejects_post_asof_source_date(
    tmp_path: Path,
) -> None:
    rows = deepcopy(_rows())
    rows[0]["source_asof_date"] = "2026-10-01"
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)

    with pytest.raises(ValueError, match="source asof is post-cutoff"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_available_score_input_requires_source_provenance(tmp_path: Path) -> None:
    rows = deepcopy(_rows())
    rows[1]["availability_status"] = "available"
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)

    with pytest.raises(ValueError, match="lacks source provenance"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


@pytest.mark.parametrize(
    "field",
    ["component_input_value_sha256", "source_record_sha256"],
)
def test_signed_availability_row_hashes_require_exact_lowercase(
    tmp_path: Path,
    field: str,
) -> None:
    rows = deepcopy(_rows())
    rows[0][field] = "A" * 64
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)

    with pytest.raises(ValueError, match="exact lowercase"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_signed_component_id_cannot_normalize_uppercase_into_expected_census(
    tmp_path: Path,
) -> None:
    rows = deepcopy(_rows())
    rows[0]["component_observation_id"] = "A" * 64
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)
    expected_ids = ["a" * 64, COMPONENT_IDS[1]]
    attestation = json.loads(attestation_bytes.decode("utf-8"))
    attestation["component_observation_ids_sha256"] = canonical_sha256(
        sorted(expected_ids)
    )
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    kwargs["expected_component_observation_ids"] = expected_ids
    kwargs["expected_attestation_sha256"] = hashlib.sha256(
        attestation_bytes
    ).hexdigest()

    with pytest.raises(ValueError, match="exact lowercase"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_signed_query_hash_requires_exact_lowercase(tmp_path: Path) -> None:
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, _rows())
    attestation = json.loads(attestation_bytes.decode("utf-8"))
    attestation["query_sha256"] = "A" * 64
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    kwargs["expected_attestation_sha256"] = hashlib.sha256(
        attestation_bytes
    ).hexdigest()

    with pytest.raises(ValueError, match="exact lowercase"):
        validate_score_input_availability_snapshot(
            **kwargs,
            bundle=_bundle(_Authority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def _capture_timing() -> dict[str, str]:
    return {
        "signal_information_cutoff_at_utc": CUTOFF,
        "source_max_information_at_utc": "2026-09-30T19:59:00+00:00",
        "source_generated_at_utc": "2026-09-30T20:01:30+00:00",
        "captured_at_utc": "2026-09-30T20:02:00+00:00",
        "entry_execution_at_utc": "2026-10-01T13:30:00+00:00",
    }


def test_score_input_availability_binds_signed_capture_timing(
    tmp_path: Path,
) -> None:
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, _rows())
    _, audit = validate_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(_Authority()),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )

    timing_audit = validate_score_input_availability_capture_chronology(
        audit,
        trusted_capture_timing=_capture_timing(),
        captured_at_utc="2026-09-30T20:02:00+00:00",
        label="Consumer test",
    )

    assert timing_audit["availability_within_signed_source_envelope_pass"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_max_information_at_utc", "2026-09-30T19:58:59+00:00"),
        ("source_generated_at_utc", "2026-09-30T20:00:59+00:00"),
        ("captured_at_utc", "2026-09-30T20:01:59+00:00"),
    ],
)
def test_score_input_availability_rejects_signed_timing_contradictions(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, _rows())
    _, audit = validate_score_input_availability_snapshot(
        **kwargs,
        bundle=_bundle(_Authority()),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )
    timing = _capture_timing()
    timing[field] = value

    with pytest.raises(ValueError, match="exceeds signed capture timing"):
        validate_score_input_availability_capture_chronology(
            audit,
            trusted_capture_timing=timing,
            captured_at_utc="2026-09-30T20:02:00+00:00",
            label="Consumer test",
        )

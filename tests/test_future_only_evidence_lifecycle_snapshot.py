from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from future_only_evidence.lifecycle_snapshot import (
    LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
    LIFECYCLE_STATUS_ASOF_POLICY,
    validate_lifecycle_capture_chronology,
    validate_lifecycle_event_snapshot,
)
from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.protocol import canonical_sha256


ASOF = "2026-09-30"
CUTOFF = "2026-09-30T20:00:00+00:00"
SCHEMA = "test_lifecycle_snapshot_v1"
TICKERS = ["AAA", "BBB"]


class _SnapshotAuthority:
    def __init__(self) -> None:
        self.calls = 0

    def verify_snapshot(
        self, payload_bytes: bytes, digest: str, payload: dict[str, object]
    ) -> bool:
        assert hashlib.sha256(payload_bytes).hexdigest() == digest
        assert json.loads(payload_bytes.decode("utf-8")) == payload
        self.calls += 1
        return True


def _bundle(
    authority: _SnapshotAuthority,
    *,
    providers: frozenset[str] = frozenset({"provider"}),
) -> SimpleNamespace:
    return SimpleNamespace(
        market_data_export=authority,
        allowed_provider_ids=providers,
        allowed_dataset_ids=frozenset({"lifecycle"}),
    )


def _rows() -> list[dict[str, object]]:
    return [
        {
            "asof_date": ASOF,
            "ticker": "AAA",
            "lifecycle_status_at_signal_cutoff": "active",
            "terminal_event_type": None,
            "terminal_event_effective_at_utc": None,
            "terminal_event_reason_code": None,
            "source_available_at_utc": "2026-09-30T19:59:00+00:00",
            "source_observation_id": "obs-AAA",
            "source_locator": "test://AAA",
            "source_record_sha256": "0" * 64,
            "provider_id": "provider",
            "dataset_id": "lifecycle",
        },
        {
            "asof_date": ASOF,
            "ticker": "BBB",
            "lifecycle_status_at_signal_cutoff": "governed_terminal_event",
            "terminal_event_type": "merger_cash",
            "terminal_event_effective_at_utc": "2026-09-29T18:00:00+00:00",
            "terminal_event_reason_code": "cash_merger",
            "source_available_at_utc": "2026-09-30T19:58:00+00:00",
            "source_observation_id": "obs-BBB",
            "source_locator": "test://BBB",
            "source_record_sha256": "1" * 64,
            "provider_id": "provider",
            "dataset_id": "lifecycle",
        },
    ]


def _artifacts(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    status_effective_through_at_utc: str = CUTOFF,
    provider_id: str = "provider",
) -> tuple[dict[str, object], bytes, bytes]:
    snapshot = {
        "schema_version": SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "snapshot_generated_at_utc": "2026-09-30T20:00:30+00:00",
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    snapshot_path = tmp_path / "lifecycle.json"
    snapshot_path.write_bytes(snapshot_bytes)
    observation_ids = sorted(str(row["source_observation_id"]) for row in rows)
    attestation = {
        "schema_version": LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
        "authority_id": "market",
        "signature_base64": "signature",
        "signed_payload_sha256": "2" * 64,
        "family": "test_family",
        "policy_id": "test_policy",
        "asof_date": ASOF,
        "lifecycle_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "lifecycle_rows_sha256": snapshot["rows_sha256"],
        "ticker_count": len(TICKERS),
        "ticker_census_sha256": canonical_sha256(TICKERS),
        "provider_id": provider_id,
        "dataset_id": "lifecycle",
        "source_max_information_at_utc": max(
            str(row["source_available_at_utc"]) for row in rows
        ),
        "status_effective_through_at_utc": status_effective_through_at_utc,
        "exported_at_utc": "2026-09-30T20:01:00+00:00",
        "status_asof_policy": LIFECYCLE_STATUS_ASOF_POLICY,
        "query_sha256": "3" * 64,
        "observation_ids_sha256": canonical_sha256(observation_ids),
    }
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_bytes(attestation_bytes)
    kwargs: dict[str, object] = {
        "path": snapshot_path,
        "expected_schema_version": SCHEMA,
        "asof_date": ASOF,
        "expected_tickers": TICKERS,
        "signal_cutoff_at_utc": CUTOFF,
        "family": "test_family",
        "policy_id": "test_policy",
        "attestation_path": attestation_path,
        "expected_attestation_sha256": hashlib.sha256(
            attestation_bytes
        ).hexdigest(),
    }
    return kwargs, snapshot_bytes, attestation_bytes


def test_lifecycle_uses_one_attested_byte_snapshot_and_exact_close(
    tmp_path: Path,
) -> None:
    authority = _SnapshotAuthority()
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, _rows())
    Path(kwargs["path"]).write_text("changed", encoding="utf-8")
    Path(kwargs["attestation_path"]).write_text("changed", encoding="utf-8")
    index, audit = validate_lifecycle_event_snapshot(
        **kwargs,
        bundle=_bundle(authority),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )
    assert set(index) == set(TICKERS)
    assert audit["exact_official_close_status_pass"] is True
    assert authority.calls == 1
    timing = {
        "signal_information_cutoff_at_utc": CUTOFF,
        "source_max_information_at_utc": CUTOFF,
        "source_generated_at_utc": "2026-09-30T20:01:00+00:00",
        "captured_at_utc": "2026-09-30T20:02:00+00:00",
        "entry_execution_at_utc": "2026-10-01T13:30:00+00:00",
    }
    assert validate_lifecycle_capture_chronology(
        audit,
        trusted_capture_timing=timing,
        captured_at_utc=timing["captured_at_utc"],
        label="test",
    )["capture_before_entry_pass"] is True


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("missing", "exact frozen ticker census"),
        ("duplicate", "duplicate ticker"),
        ("lowercase", "canonical uppercase"),
        ("stale_active", "active lifecycle assertion is stale"),
        ("active_terminal_fields", "active lifecycle row has terminal fields"),
        ("post_cutoff_terminal", "post-cutoff knowledge"),
    ],
)
def test_lifecycle_rejects_census_and_time_mutations(
    tmp_path: Path, case: str, match: str
) -> None:
    rows = deepcopy(_rows())
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows[1]["ticker"] = "AAA"
    elif case == "lowercase":
        rows[0]["ticker"] = "aaa"
    elif case == "stale_active":
        rows[0]["source_available_at_utc"] = "2026-09-29T19:58:59+00:00"
    elif case == "active_terminal_fields":
        rows[0]["terminal_event_type"] = "delisting"
    elif case == "post_cutoff_terminal":
        rows[1]["terminal_event_effective_at_utc"] = (
            "2026-09-30T20:01:00+00:00"
        )
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, rows)
    with pytest.raises(ValueError, match=match):
        validate_lifecycle_event_snapshot(
            **kwargs,
            bundle=_bundle(_SnapshotAuthority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


def test_lifecycle_rejects_unregistered_source_and_non_close_attestation(
    tmp_path: Path,
) -> None:
    rows = _rows()
    for row in rows:
        row["provider_id"] = "invented"
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(
        tmp_path, rows, provider_id="invented"
    )
    with pytest.raises(ValueError, match="canonical trust allowlists"):
        validate_lifecycle_event_snapshot(
            **kwargs,
            bundle=_bundle(_SnapshotAuthority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(
        tmp_path,
        _rows(),
        status_effective_through_at_utc="2026-09-30T19:59:00+00:00",
    )
    with pytest.raises(ValueError, match="chronology"):
        validate_lifecycle_event_snapshot(
            **kwargs,
            bundle=_bundle(_SnapshotAuthority()),
            snapshot_bytes=snapshot_bytes,
            attestation_bytes=attestation_bytes,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_max_information_at_utc", "2026-09-30T19:58:00+00:00"),
        ("source_generated_at_utc", "2026-09-30T20:00:00+00:00"),
        ("captured_at_utc", "2026-09-30T20:00:30+00:00"),
        ("entry_execution_at_utc", "2026-09-30T20:01:30+00:00"),
    ],
)
def test_evaluator_chronology_rejects_handcrafted_signed_capture_timing(
    tmp_path: Path, field: str, value: str
) -> None:
    kwargs, snapshot_bytes, attestation_bytes = _artifacts(tmp_path, _rows())
    _, audit = validate_lifecycle_event_snapshot(
        **kwargs,
        bundle=_bundle(_SnapshotAuthority()),
        snapshot_bytes=snapshot_bytes,
        attestation_bytes=attestation_bytes,
    )
    timing = {
        "signal_information_cutoff_at_utc": CUTOFF,
        "source_max_information_at_utc": CUTOFF,
        "source_generated_at_utc": "2026-09-30T20:01:00+00:00",
        "captured_at_utc": "2026-09-30T20:02:00+00:00",
        "entry_execution_at_utc": "2026-10-01T13:30:00+00:00",
    }
    timing[field] = value
    with pytest.raises(ValueError, match="signed capture timing"):
        validate_lifecycle_capture_chronology(
            audit,
            trusted_capture_timing=timing,
            captured_at_utc=timing["captured_at_utc"],
            label="evaluator",
        )

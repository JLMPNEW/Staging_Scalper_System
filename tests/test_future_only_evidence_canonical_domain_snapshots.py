from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from future_only_evidence import canonical_domain
from future_only_evidence.canonical_domain import validate_canonical_outcome_attestations


def _snapshot(path: Path, payload: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return raw, hashlib.sha256(raw).hexdigest()


def test_registry_and_outcome_attestations_use_exact_evaluator_snapshots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    domain_sha = "1" * 64
    latest_head = "2" * 64
    registry_path = tmp_path / "registry.json"
    registry_raw, registry_sha = _snapshot(
        registry_path,
        {"policy_id": "policy-v1", "complete_through_asof": "2026-09-30"},
    )
    registry_receipt_path = tmp_path / "registry-receipt.json"
    registry_receipt_raw, registry_receipt_sha = _snapshot(
        registry_receipt_path,
        {"domain_contract_sha256": domain_sha},
    )
    outcome_receipt_path = tmp_path / "outcome-receipt.json"
    outcome_receipt_raw, outcome_receipt_sha = _snapshot(
        outcome_receipt_path,
        {
            "domain_contract_sha256": domain_sha,
            "capture_registry_sha256": registry_sha,
            "capture_registry_timestamp_receipt_sha256": "3" * 64,
            "latest_capture_log_head_sha256": latest_head,
            "benchmark_ticker": "XLP",
            "anchored_at_utc": "2026-10-02T12:00:00+00:00",
        },
    )
    # Simulate all three paths changing after the evaluator bound their bytes.
    registry_path.write_text("{}", encoding="utf-8")
    registry_receipt_path.write_text("{}", encoding="utf-8")
    outcome_receipt_path.write_text("{}", encoding="utf-8")
    verified: list[bytes] = []

    class SnapshotOnlyAuthority:
        def verify_snapshot(self, raw: bytes, *_args: object) -> bool:
            verified.append(raw)
            return True

        def verify(self, *_args: object) -> bool:
            raise AssertionError("path-based receipt reread is forbidden")

    bundle = SimpleNamespace(
        family="consumer_defensive",
        evidence_seal=SnapshotOnlyAuthority(),
    )
    timestamp_subjects: list[bytes] = []

    def fake_timestamp(**kwargs):
        timestamp_subjects.append(kwargs["subject_snapshot_bytes"])
        if kwargs["expected_subject_role"] == "capture_registry":
            return {
                "timestamp_receipt_sha256": "3" * 64,
                "log_sequence": 11,
                "observed_at_utc": "2026-10-02T11:00:00+00:00",
            }
        return {
            "timestamp_receipt_sha256": "4" * 64,
            "log_sequence": 12,
            "observed_at_utc": "2026-10-02T13:00:00+00:00",
        }

    monkeypatch.setattr(canonical_domain, "validate_external_timestamp", fake_timestamp)
    monkeypatch.setattr(
        canonical_domain,
        "validate_market_data_export_receipt",
        lambda **_kwargs: {
            "market_data_export_attestation_pass": True,
            "exported_at_utc": "2026-10-02T12:30:00+00:00",
        },
    )
    audit = validate_canonical_outcome_attestations(
        outcome_receipt_path=outcome_receipt_path,
        outcome_timestamp_receipt_path=tmp_path / "outcome-timestamp.json",
        expected_outcome_timestamp_receipt_sha256="5" * 64,
        market_export_receipt_path=tmp_path / "market.json",
        expected_market_export_receipt_sha256="6" * 64,
        expected_outcome_receipt_sha256=outcome_receipt_sha,
        source_sha256={"total_return_bars": "7" * 64},
        capture_registry_path=registry_path,
        expected_capture_registry_sha256=registry_sha,
        capture_registry_receipt_path=registry_receipt_path,
        expected_capture_registry_receipt_sha256=registry_receipt_sha,
        capture_registry_timestamp_receipt_path=tmp_path / "registry-timestamp.json",
        expected_capture_registry_timestamp_receipt_sha256="8" * 64,
        expected_domain_contract_sha256=domain_sha,
        expected_latest_capture_log_head_sha256=latest_head,
        expected_latest_capture_log_sequence=10,
        bundle=bundle,  # type: ignore[arg-type]
        evaluated_at_utc="2026-10-02T14:00:00+00:00",
        latest_exit_execution_at_utc="2026-10-01T13:30:00+00:00",
        capture_registry_snapshot_bytes=registry_raw,
        capture_registry_receipt_snapshot_bytes=registry_receipt_raw,
        outcome_receipt_snapshot_bytes=outcome_receipt_raw,
    )
    assert verified == [registry_receipt_raw, outcome_receipt_raw]
    assert timestamp_subjects == [registry_raw, outcome_receipt_raw]
    assert audit["append_only_registry_head_bound_pass"] is True

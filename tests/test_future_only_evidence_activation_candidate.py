from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from future_only_evidence import activation_candidate
from future_only_evidence.activation_candidate import build_activation_candidate
from future_only_evidence.protocol import canonical_sha256


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict[str, object]) -> str:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _passing_transport_evaluation() -> dict[str, object]:
    verdict = {
        "pass": True,
        "action": "eligible_for_independent_review",
        "production_activation_authorized": False,
        "optimizer_cap": 0.0,
    }
    body: dict[str, object] = {
        "schema_version": "transportation_future_only_evaluation_v6",
        "family": "transportation",
        "evaluated_at_utc": "2028-01-03T20:00:00+00:00",
        "domain_contract_sha256": "1" * 64,
        "sleeve_independent_verdicts": [{"sleeve_id": "airlines", **verdict}],
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    body["payload_sha256"] = canonical_sha256(body)
    return body


def test_manual_activation_bridge_stays_blocked_without_review_authority(
    tmp_path: Path,
) -> None:
    evaluation = tmp_path / "evaluation.json"
    evaluation_hash = _write_json(evaluation, _passing_transport_evaluation())
    output = tmp_path / "candidate.json"
    with pytest.raises(ValueError, match="unconfigured"):
        build_activation_candidate(
            family="transportation",
            scope_id="airlines",
            evaluation_path=evaluation,
            expected_evaluation_sha256=evaluation_hash,
            review_receipt_path=tmp_path / "missing-review.json",
            expected_review_receipt_sha256="0" * 64,
            review_public_key_path=tmp_path / "missing-review.pem",
            generated_at_utc="2028-01-04T20:00:00+00:00",
        )
    assert not output.exists()


def test_tampered_evaluation_is_rejected_before_independent_review(
    tmp_path: Path,
) -> None:
    evaluation = tmp_path / "evaluation.json"
    payload = _passing_transport_evaluation()
    original_hash = _write_json(evaluation, payload)
    payload["portfolio_write_enabled"] = True
    _write_json(evaluation, payload)
    with pytest.raises(ValueError, match="bytes changed"):
        build_activation_candidate(
            family="transportation",
            scope_id="airlines",
            evaluation_path=evaluation,
            expected_evaluation_sha256=original_hash,
            review_receipt_path=tmp_path / "missing-review.json",
            expected_review_receipt_sha256="0" * 64,
            review_public_key_path=tmp_path / "missing-review.pem",
            generated_at_utc="2028-01-04T20:00:00+00:00",
        )


def test_review_receipt_signature_verifies_the_single_read_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_payload = _passing_transport_evaluation()
    evaluation_payload["canonical_trust_audit"] = {
        "evidence_seal": {
            "authority_id": "evidence-authority",
            "public_key_spki_sha256": "1" * 64,
        },
        "timestamp_log": {
            "authority_id": "timestamp-authority",
            "public_key_spki_sha256": "2" * 64,
        },
        "market_data_export": {
            "authority_id": "market-authority",
            "public_key_spki_sha256": "3" * 64,
        },
    }
    evaluation_payload.pop("payload_sha256")
    evaluation_payload["payload_sha256"] = canonical_sha256(evaluation_payload)
    evaluation_path = tmp_path / "evaluation.json"
    evaluation_hash = _write_json(evaluation_path, evaluation_payload)
    verdict = evaluation_payload["sleeve_independent_verdicts"][0]
    review_payload = {
        "schema_version": activation_candidate.REVIEW_RECEIPT_SCHEMA,
        "family": "transportation",
        "scope_id": "airlines",
        "evaluation_sha256": evaluation_hash,
        "evaluation_payload_sha256": evaluation_payload["payload_sha256"],
        "domain_contract_sha256": evaluation_payload["domain_contract_sha256"],
        "scope_verdict_sha256": canonical_sha256(verdict),
        "decision": "accept_for_manual_activation_change_control",
        "automatic_config_write_authorized": False,
        "reviewed_at_utc": "2028-01-04T20:00:00+00:00",
    }
    review_path = tmp_path / "review.json"
    review_hash = _write_json(review_path, review_payload)
    expected_review_bytes = review_path.read_bytes()
    verification_calls: list[str] = []

    class SnapshotOnlyAuthority:
        def identity(self) -> dict[str, str]:
            return {
                "authority_id": "independent-review-authority",
                "public_key_spki_sha256": "4" * 64,
            }

        def verify_snapshot(
            self,
            raw: bytes,
            actual_sha256: str,
            payload: dict[str, object],
        ) -> bool:
            assert raw == expected_review_bytes
            assert actual_sha256 == review_hash
            assert payload == review_payload
            verification_calls.append(actual_sha256)
            return True

        def verify(self, *_args: object, **_kwargs: object) -> bool:
            raise AssertionError("path-based receipt reread is forbidden")

    monkeypatch.setattr(
        activation_candidate,
        "_review_authority",
        lambda *_args, **_kwargs: (SnapshotOnlyAuthority(), "5" * 64),
    )

    candidate = build_activation_candidate(
        family="transportation",
        scope_id="airlines",
        evaluation_path=evaluation_path,
        expected_evaluation_sha256=evaluation_hash,
        review_receipt_path=review_path,
        expected_review_receipt_sha256=review_hash,
        review_public_key_path=tmp_path / "not-reread.pem",
        generated_at_utc="2028-01-05T20:00:00+00:00",
    )

    assert verification_calls == [review_hash]
    assert candidate["automatic_config_write_authorized"] is False
    assert candidate["optimizer_cap"] == 0.0


def test_activation_candidate_cli_exposes_only_manual_zero_cap_bridge() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "future_only_evidence.activation_cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.casefold()
    assert "--authority-registry" not in completed.stdout

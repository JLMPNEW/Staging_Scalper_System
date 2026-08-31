from __future__ import annotations

import base64
import csv
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from future_only_evidence.capture_integrity import validate_capture_receipt_timing
from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.trusted_receipts import PinnedEd25519Authority


def _fixture(tmp_path: Path, captured_at: str) -> tuple[Path, Path, PinnedEd25519Authority]:
    calendar = tmp_path / "calendar.csv"
    with calendar.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["session_date", "entry_execution_at_utc"])
        writer.writeheader()
        writer.writerow(
            {
                "session_date": "2026-08-25",
                "entry_execution_at_utc": "2026-08-25T13:30:00+00:00",
            }
        )
    private = Ed25519PrivateKey.generate()
    key = tmp_path / "key.pem"
    key.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    unsigned = {
        "schema_version": "future_signal_capture_receipt_v1",
        "authority_id": "independent-risk",
        "family": "transportation",
        "asof_date": "2026-08-24",
        "capture_date": "2026-08-24",
        "captured_at_utc": captured_at,
        "signal_information_cutoff_at_utc": "2026-08-24T20:00:00+00:00",
        "entry_session_date": "2026-08-25",
        "entry_execution_at_utc": "2026-08-25T13:30:00+00:00",
        "trading_calendar_sha256": file_sha256(calendar),
        "source_sha256": {"unused": "a" * 64},
        "signal_rows_sha256": "b" * 64,
    }
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    payload = {
        **unsigned,
        "signed_payload_sha256": canonical_sha256(unsigned),
        "signature_base64": base64.b64encode(private.sign(encoded)).decode("ascii"),
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    authority = PinnedEd25519Authority("independent-risk", key, file_sha256(key))
    return receipt, calendar, authority


def test_signed_capture_precedes_exact_entry_timestamp(tmp_path: Path) -> None:
    receipt, calendar, authority = _fixture(tmp_path, "2026-08-24T21:00:00+00:00")
    result = validate_capture_receipt_timing(
        receipt_path=receipt,
        authority=authority,
        asof_date="2026-08-24",
        trading_calendar_path=calendar,
    )
    assert result["capture_before_entry_pass"] is True


def test_post_entry_receipt_is_rejected(tmp_path: Path) -> None:
    receipt, calendar, authority = _fixture(tmp_path, "2026-08-25T14:00:00+00:00")
    with pytest.raises(ValueError, match="before entry execution"):
        validate_capture_receipt_timing(
            receipt_path=receipt,
            authority=authority,
            asof_date="2026-08-24",
            trading_calendar_path=calendar,
        )


@pytest.mark.parametrize(
    ("asof_date", "captured_at"),
    [
        ("2026-08-24T00:00:00Z", "2026-08-24T21:00:00+00:00"),
        ("2026-08-24", "2026-08-24 21:00:00+00:00"),
        ("2026-08-24", "2026-08-24T16:00:00-05:00"),
    ],
)
def test_capture_timing_rejects_noncanonical_dates_and_timestamps(
    tmp_path: Path,
    asof_date: str,
    captured_at: str,
) -> None:
    receipt, calendar, authority = _fixture(tmp_path, captured_at)
    with pytest.raises(ValueError, match="exact (YYYY-MM-DD|RFC3339 UTC)"):
        validate_capture_receipt_timing(
            receipt_path=receipt,
            authority=authority,
            asof_date=asof_date,
            trading_calendar_path=calendar,
        )

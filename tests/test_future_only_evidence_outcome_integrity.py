from __future__ import annotations

import base64
import csv
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from future_only_evidence.outcome_integrity import validate_and_recompute_outcomes
from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.trusted_receipts import PinnedEd25519Authority


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _fixture(tmp_path: Path, *, submitted_gross: float, submitted_benchmark: float):
    bars = _write_csv(
        tmp_path / "bars.csv",
        ["ticker", "session_date", "total_return_index"],
        [
            {"ticker": "AAA", "session_date": "2026-08-25", "total_return_index": 100},
            {"ticker": "AAA", "session_date": "2026-08-26", "total_return_index": 110},
            {"ticker": "XLP", "session_date": "2026-08-25", "total_return_index": 100},
            {"ticker": "XLP", "session_date": "2026-08-26", "total_return_index": 102},
            {"ticker": "SPY", "session_date": "2026-08-25", "total_return_index": 100},
            {"ticker": "SPY", "session_date": "2026-08-26", "total_return_index": 105},
        ],
    )
    terminals = _write_csv(
        tmp_path / "terminal.csv",
        [
            "ticker",
            "exit_date",
            "terminal_event_status",
            "total_return_index_includes_terminal_proceeds_flag",
        ],
        [],
    )
    calendar = (tmp_path / "calendar.csv")
    calendar.write_text("session_date\n2026-08-25\n2026-08-26\n", encoding="utf-8")
    membership = (tmp_path / "membership.csv")
    membership.write_text("ticker\nAAA\n", encoding="utf-8")
    sources = {
        "total_return_bars": bars,
        "terminal_events": terminals,
        "trading_calendar": calendar,
        "membership_history": membership,
    }
    source_hashes = {role: file_sha256(path) for role, path in sources.items()}
    capture_id = "a" * 64
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "capture_id": capture_id,
                "captured_at_utc": "2026-08-24T21:00:00+00:00",
                "trusted_capture_timing": {
                    "entry_execution_at_utc": "2026-08-25T13:30:00+00:00"
                },
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {
            "capture_id": capture_id,
            "ticker": "AAA",
            "horizon_sessions": 1,
            "entry_date": "2026-08-25",
            "exit_date": "2026-08-26",
            "entry_execution_at_utc": "2026-08-25T13:30:00+00:00",
            "outcome_available_at_utc": "2026-08-26T22:00:00+00:00",
            "terminal_event_status": "none",
            "stock_total_return": 0.10,
            "benchmark_total_return": submitted_benchmark,
            "gross_return": submitted_gross,
        }
    ]
    outcome = tmp_path / "outcome.json"
    outcome.write_text(
        json.dumps(
            {
                "rows": rows,
                "rows_sha256": canonical_sha256(rows),
                "source_sha256": source_hashes,
            }
        ),
        encoding="utf-8",
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
        "schema_version": "future_outcome_receipt_v1",
        "authority_id": "independent-risk",
        "family": "consumer_defensive",
        "outcome_rows_sha256": canonical_sha256(rows),
        "source_sha256": source_hashes,
        "capture_ids": [capture_id],
        "outcomes_available_at_utc": "2026-08-26T23:00:00+00:00",
    }
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    receipt_payload = {
        **unsigned,
        "signed_payload_sha256": canonical_sha256(unsigned),
        "signature_base64": base64.b64encode(private.sign(encoded)).decode("ascii"),
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    authority = PinnedEd25519Authority("independent-risk", key, file_sha256(key))
    return capture, outcome, sources, receipt, authority


def test_submitted_return_is_recomputed_not_trusted(tmp_path: Path) -> None:
    capture, outcome, sources, receipt, authority = _fixture(
        tmp_path,
        submitted_gross=0.50,
        submitted_benchmark=0.02,
    )
    with pytest.raises(ValueError, match="differs from raw"):
        validate_and_recompute_outcomes(
            family="consumer_defensive",
            capture_paths=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=authority,
            benchmark_ticker="XLP",
        )


def test_spy_cannot_substitute_for_registered_xlp_benchmark(tmp_path: Path) -> None:
    capture, outcome, sources, receipt, authority = _fixture(
        tmp_path,
        submitted_gross=0.05,
        submitted_benchmark=0.05,
    )
    with pytest.raises(ValueError, match="differs from raw"):
        validate_and_recompute_outcomes(
            family="consumer_defensive",
            capture_paths=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=authority,
            benchmark_ticker="XLP",
        )

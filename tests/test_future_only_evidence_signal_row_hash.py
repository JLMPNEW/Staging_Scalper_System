from __future__ import annotations

import json
from pathlib import Path

import pytest

from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.protocol_v3 import validate_capture_payload_v3


def test_outer_rehash_cannot_hide_tampered_signal_row_hash(tmp_path: Path) -> None:
    del tmp_path
    row = {
        "ticker": "A",
        "sleeve_id": "s",
        "group_id": "g",
        "score": 1.0,
        "rank": 1.0,
        "ranking_mode": "ranked",
        "eligible_flag": 1,
        "selected_top_flag": 1,
        "selected_bottom_flag": 0,
    }
    row["signal_row_sha256"] = canonical_sha256(row)
    body = {
        "schema_version": "future_only_signal_capture_v1",
        "state": "captured_pending_outcomes",
        "evidence_class": "prospective_future_only",
        "family": "x",
        "policy_id": "p",
        "asof_date": "2026-08-24",
        "outcomes_present_at_capture": False,
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "signal_rows": [row],
        "signal_rows_sha256": canonical_sha256([row]),
    }
    body["capture_id"] = canonical_sha256(body)
    body["payload_sha256"] = canonical_sha256(body)
    tampered = json.loads(json.dumps(body))
    tampered["signal_rows"][0]["signal_row_sha256"] = "0" * 64
    tampered["signal_rows_sha256"] = canonical_sha256(tampered["signal_rows"])
    tampered.pop("capture_id")
    tampered.pop("payload_sha256")
    tampered["capture_id"] = canonical_sha256(tampered)
    tampered["payload_sha256"] = canonical_sha256(tampered)
    with pytest.raises(ValueError, match="does not bind exact row"):
        validate_capture_payload_v3(tampered)

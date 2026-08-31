from __future__ import annotations

import json
from pathlib import Path

from industrials.transportation import future_oos_protocol_v2


def test_equal_weight_scope_is_explicitly_na_and_excluded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "capture_id": "a" * 64,
                "signal_rows": [
                    {
                        "ticker": "UPS",
                        "sleeve_id": "north_american_surface_freight_and_logistics_v5",
                        "group_id": "integrated_parcel",
                        "ranking_mode": "eligibility_equal_weight",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    base = {
        "schema_version": "future_only_evidence_evaluation_v1",
        "scope_verdicts": [],
        "sleeve_verdicts": [],
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "payload_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        future_oos_protocol_v2,
        "evaluate_future_evidence",
        lambda **_: dict(base),
    )
    result = future_oos_protocol_v2.evaluate(
        capture_paths=[capture],
        outcome_path=tmp_path / "unused.json",
        evaluation_at_utc="2027-01-01T00:00:00+00:00",
    )
    rows = result["scope_verdicts"]
    assert len(rows) == 2
    assert {row["horizon_sessions"] for row in rows} == {21, 63}
    assert all(row["applicability"] == "not_applicable" for row in rows)
    assert all(row["pass"] is None for row in rows)
    assert result["group_scope_audit"] == {
        "applicable_predictive_verdict_count": 0,
        "not_applicable_equal_weight_verdict_count": 2,
        "not_applicable_excluded_from_group_pass_denominator": True,
    }

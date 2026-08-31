from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.protocol import canonical_sha256
from future_only_evidence.lifecycle_snapshot import (
    LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
    LIFECYCLE_STATUS_ASOF_POLICY,
)
from industrials.transportation.future_oos_activation_v6 import (
    GROUP_MODES,
    GROUP_TICKERS,
    GROUP_WEIGHTS,
    LIFECYCLE_EVENT_SCHEMA_V6,
    POLICY_ID,
    REQUIRED_PLAN_ROLES,
    SCORE_REPLAY_CONTRACT,
)
from industrials.transportation.future_oos_capture_v6 import (
    MEMBERSHIP_SCHEMA_V6,
    REQUIRED_CAPTURE_ROLES_V6,
    _utc as _transport_capture_utc,
    validate_exact_transport_census,
)
from industrials.transportation.future_oos_score_lineage_v1 import SOURCE_ROLES


ASOF = "2026-09-30"
CUTOFF = "2026-09-30T20:00:00+00:00"


class _AcceptingAuthority:
    def verify_snapshot(self, *_args: object) -> bool:
        return True


TRUST_BUNDLE = SimpleNamespace(
    market_data_export=_AcceptingAuthority(),
    allowed_provider_ids=frozenset({"test_provider"}),
    allowed_dataset_ids=frozenset({"test_lifecycle"}),
)


def _sleeve(group: str) -> str:
    return next(sleeve for sleeve, weights in GROUP_WEIGHTS.items() if group in weights)


def _case(
    *, tanker_lifecycle_eligible: int = 11
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    signals: list[dict[str, object]] = []
    members: list[dict[str, object]] = []
    replay: dict[str, dict[str, object]] = {}
    for group, tickers in GROUP_TICKERS.items():
        for rank, ticker in enumerate(tickers, start=1):
            lifecycle_flag = int(
                group != "oil_tankers" or rank <= tanker_lifecycle_eligible
            )
            lifecycle = "active" if lifecycle_flag else "governed_terminal_event"
            signals.append(
                {
                    "asof_date": ASOF,
                    "ticker": ticker,
                    "sleeve_id": _sleeve(group),
                    "group_id": group,
                    "score": float(len(tickers) - rank),
                    "rank": rank,
                    "ranking_mode": GROUP_MODES[group],
                    "eligible_flag": 1,
                    "predictive_eligible_flag": int(GROUP_MODES[group] == "ranked"),
                    "selected_top_flag": 0,
                    "selected_bottom_flag": 0,
                }
            )
            final_reasons = (
                [] if lifecycle_flag else ["lifecycle_governed_terminal_event"]
            )
            members.append(
                {
                    "asof_date": ASOF,
                    "ticker": ticker,
                    "sleeve_id": _sleeve(group),
                    "group_id": group,
                    "lifecycle_status_at_signal_cutoff": lifecycle,
                    "lifecycle_eligible_flag": lifecycle_flag,
                    "model_data_eligible_flag": 1,
                    "model_data_exclusion_reason_codes": [],
                    "final_signal_eligible_flag": lifecycle_flag,
                    "final_signal_exclusion_reason_codes": final_reasons,
                }
            )
            replay[ticker] = {
                "model_data_eligible_flag": 1,
                "model_data_exclusion_reason_codes": [],
            }
    return signals, members, {"model_data_eligibility_by_ticker": replay}


def _membership(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": MEMBERSHIP_SCHEMA_V6,
                "evidence_role": PROSPECTIVE_ROLE,
                "asof_date": ASOF,
                "rows": rows,
                "rows_sha256": canonical_sha256(rows),
            }
        ),
        encoding="utf-8",
    )
    return path


def _lifecycle(
    tmp_path: Path, rows: list[dict[str, object]]
) -> dict[str, object]:
    tickers = sorted(str(row["ticker"]) for row in rows)
    lifecycle_rows = []
    for row in rows:
        ticker = str(row["ticker"])
        terminal = row["lifecycle_status_at_signal_cutoff"] != "active"
        lifecycle_rows.append(
            {
                "asof_date": ASOF,
                "ticker": ticker,
                "lifecycle_status_at_signal_cutoff": row[
                    "lifecycle_status_at_signal_cutoff"
                ],
                "terminal_event_type": "delisting" if terminal else None,
                "terminal_event_effective_at_utc": (
                    "2026-09-29T18:00:00+00:00" if terminal else None
                ),
                "terminal_event_reason_code": "delisting" if terminal else None,
                "source_available_at_utc": "2026-09-30T19:59:00+00:00",
                "source_observation_id": f"obs-{ticker}",
                "source_locator": f"test://lifecycle/{ticker}",
                "source_record_sha256": "0" * 64,
                "provider_id": "test_provider",
                "dataset_id": "test_lifecycle",
            }
        )
    snapshot = {
        "schema_version": LIFECYCLE_EVENT_SCHEMA_V6,
        "evidence_role": PROSPECTIVE_ROLE,
        "asof_date": ASOF,
        "snapshot_generated_at_utc": "2026-09-30T20:00:30+00:00",
        "rows": lifecycle_rows,
        "rows_sha256": canonical_sha256(lifecycle_rows),
    }
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    snapshot_path = tmp_path / "lifecycle.json"
    snapshot_path.write_bytes(snapshot_bytes)
    attestation = {
        "schema_version": LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
        "authority_id": "test_market_authority",
        "signature_base64": "test",
        "signed_payload_sha256": "1" * 64,
        "family": "transportation",
        "policy_id": POLICY_ID,
        "asof_date": ASOF,
        "lifecycle_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "lifecycle_rows_sha256": snapshot["rows_sha256"],
        "ticker_count": len(tickers),
        "ticker_census_sha256": canonical_sha256(tickers),
        "provider_id": "test_provider",
        "dataset_id": "test_lifecycle",
        "source_max_information_at_utc": "2026-09-30T19:59:00+00:00",
        "status_effective_through_at_utc": CUTOFF,
        "exported_at_utc": "2026-09-30T20:01:00+00:00",
        "status_asof_policy": LIFECYCLE_STATUS_ASOF_POLICY,
        "query_sha256": "2" * 64,
        "observation_ids_sha256": canonical_sha256(
            sorted(f"obs-{ticker}" for ticker in tickers)
        ),
    }
    attestation_bytes = json.dumps(attestation).encode("utf-8")
    attestation_path = tmp_path / "lifecycle-attestation.json"
    attestation_path.write_bytes(attestation_bytes)
    return {
        "lifecycle_snapshot_path": snapshot_path,
        "lifecycle_attestation_path": attestation_path,
        "expected_lifecycle_attestation_sha256": hashlib.sha256(
            attestation_bytes
        ).hexdigest(),
        "trust_bundle": TRUST_BUNDLE,
        "signal_cutoff_at_utc": CUTOFF,
    }


def test_signed_score_input_availability_roles_are_canonical() -> None:
    baseline_roles = {
        "score_input_availability_baseline_snapshot",
        "score_input_availability_baseline_attestation",
    }
    dynamic_roles = {
        "score_input_availability_snapshot",
        "score_input_availability_attestation",
    }

    assert baseline_roles <= REQUIRED_PLAN_ROLES
    assert baseline_roles | dynamic_roles <= REQUIRED_CAPTURE_ROLES_V6
    assert SCORE_REPLAY_CONTRACT["source_roles"] == sorted(SOURCE_ROLES)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-30 20:00:00+00:00",
        "2026-09-30T15:00:00-05:00",
        "2026-09-30T20:00Z",
    ],
)
def test_transport_capture_route_rejects_noncanonical_utc(value: str) -> None:
    with pytest.raises(ValueError, match="exact RFC3339 UTC"):
        _transport_capture_utc(value, label="capture time")


def test_two_name_tanker_subset_cannot_pass_frozen_minimum(tmp_path: Path) -> None:
    signals, members, replay = _case(tanker_lifecycle_eligible=2)
    with pytest.raises(ValueError, match="frozen minimum"):
        validate_exact_transport_census(
            signals=signals,
            membership_path=_membership(tmp_path / "members.json", members),
            asof_date=ASOF,
            score_replay_audit=replay,
            **_lifecycle(tmp_path, members),
        )


def test_arbitrary_replacement_ticker_cannot_change_frozen_group_census(
    tmp_path: Path,
) -> None:
    signals, members, replay = _case()
    replaced = GROUP_TICKERS["rail_networks"][0]
    next(row for row in signals if row["ticker"] == replaced)["ticker"] = "FAKE"
    next(row for row in members if row["ticker"] == replaced)["ticker"] = "FAKE"
    with pytest.raises(ValueError, match="frozen.*ticker census"):
        validate_exact_transport_census(
            signals=signals,
            membership_path=_membership(tmp_path / "members.json", members),
            asof_date=ASOF,
            score_replay_audit=replay,
            **_lifecycle(tmp_path, members),
        )

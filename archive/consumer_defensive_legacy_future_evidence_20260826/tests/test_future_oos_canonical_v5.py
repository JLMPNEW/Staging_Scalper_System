from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from consumer_defensive.core.future_oos_capture_v5 import (
    MEMBERSHIP_SCHEMA_V5,
    _utc as _consumer_capture_utc,
    reconcile_exact_registered_census,
)
from consumer_defensive.core.future_oos_plan_v5 import (
    CANDIDATE_SCHEMA,
    LIFECYCLE_EVENT_SCHEMA_V5,
    POLICY_ID,
    UNIVERSE_SCHEMA,
    _candidate_census,
)
from future_only_evidence.lifecycle_snapshot import (
    LIFECYCLE_SOURCE_ATTESTATION_SCHEMA,
    LIFECYCLE_STATUS_ASOF_POLICY,
)
from future_only_evidence.prospective_contracts import PROSPECTIVE_ROLE
from future_only_evidence.protocol import canonical_sha256


ASOF = "2026-09-30"
TICKERS = ["AAA", "BBB", "CCC"]
CUTOFF = "2026-09-30T20:00:00+00:00"


class _AcceptingAuthority:
    def verify_snapshot(self, *_args: object) -> bool:
        return True


TRUST_BUNDLE = SimpleNamespace(
    market_data_export=_AcceptingAuthority(),
    allowed_provider_ids=frozenset({"test_provider"}),
    allowed_dataset_ids=frozenset({"test_lifecycle"}),
)


def _plan_audit() -> dict[str, object]:
    return {
        "candidate_census": {
            "candidate_tickers": TICKERS,
            "cohort_tickers": {"beverages": TICKERS},
        },
        "domain_contract": {
            "policy_id": POLICY_ID,
            "cohort_minimum_cross_sections": {"beverages": 2},
        },
    }


def _signals(model_flags: dict[str, int]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for rank, ticker in enumerate(TICKERS, start=1):
        eligible = model_flags[ticker]
        rows.append(
            {
                "asof_date": ASOF,
                "ticker": ticker,
                "sleeve_id": "beverages",
                "group_id": "beverages",
                "score": float(4 - rank),
                "rank": rank,
                "ranking_mode": "ranked",
                "eligible_flag": eligible,
                "predictive_eligible_flag": eligible,
                "selected_top_flag": int(ticker == "AAA" and eligible == 1),
                "selected_bottom_flag": int(ticker == "CCC" and eligible == 1),
            }
        )
    return rows


def _replay(model_flags: dict[str, int]) -> dict[str, object]:
    return {
        "model_data_eligibility_by_ticker": {
            ticker: {
                "model_data_eligible_flag": flag,
                "model_data_exclusion_reason_codes": (
                    [] if flag else ["missing_required_financial_inputs"]
                ),
            }
            for ticker, flag in model_flags.items()
        }
    }


def _member(
    ticker: str,
    *,
    lifecycle: str,
    model_flag: int,
) -> dict[str, object]:
    lifecycle_flag = int(lifecycle == "active")
    model_reasons = [] if model_flag else ["missing_required_financial_inputs"]
    final_flag = lifecycle_flag & model_flag
    final_reasons = sorted(
        set(
            ([] if lifecycle_flag else ["lifecycle_governed_terminal_event"])
            + model_reasons
        )
    )
    return {
        "asof_date": ASOF,
        "ticker": ticker,
        "cohort_id": "beverages",
        "group_id": "beverages",
        "lifecycle_status_at_signal_cutoff": lifecycle,
        "lifecycle_eligible_flag": lifecycle_flag,
        "model_data_eligible_flag": model_flag,
        "model_data_exclusion_reason_codes": model_reasons,
        "final_signal_eligible_flag": final_flag,
        "final_signal_exclusion_reason_codes": final_reasons,
    }


def _membership(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": MEMBERSHIP_SCHEMA_V5,
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
                "terminal_event_type": "merger_cash" if terminal else None,
                "terminal_event_effective_at_utc": (
                    "2026-09-29T18:00:00+00:00" if terminal else None
                ),
                "terminal_event_reason_code": "cash_merger" if terminal else None,
                "source_available_at_utc": "2026-09-30T19:59:00+00:00",
                "source_observation_id": f"obs-{ticker}",
                "source_locator": f"test://lifecycle/{ticker}",
                "source_record_sha256": "0" * 64,
                "provider_id": "test_provider",
                "dataset_id": "test_lifecycle",
            }
        )
    snapshot = {
        "schema_version": LIFECYCLE_EVENT_SCHEMA_V5,
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
        "family": "consumer_defensive",
        "policy_id": POLICY_ID,
        "asof_date": ASOF,
        "lifecycle_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "lifecycle_rows_sha256": snapshot["rows_sha256"],
        "ticker_count": len(TICKERS),
        "ticker_census_sha256": canonical_sha256(sorted(TICKERS)),
        "provider_id": "test_provider",
        "dataset_id": "test_lifecycle",
        "source_max_information_at_utc": "2026-09-30T19:59:00+00:00",
        "status_effective_through_at_utc": CUTOFF,
        "exported_at_utc": "2026-09-30T20:01:00+00:00",
        "status_asof_policy": LIFECYCLE_STATUS_ASOF_POLICY,
        "query_sha256": "2" * 64,
        "observation_ids_sha256": canonical_sha256(
            sorted(f"obs-{ticker}" for ticker in TICKERS)
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


def test_active_low_quality_name_is_excluded_only_by_deterministic_replay(
    tmp_path: Path,
) -> None:
    model_flags = {"AAA": 0, "BBB": 1, "CCC": 1}
    rows = [
        _member(ticker, lifecycle="active", model_flag=model_flags[ticker])
        for ticker in TICKERS
    ]
    resolved, audit = reconcile_exact_registered_census(
        asof_date=ASOF,
        signals=_signals(model_flags),
        membership_path=_membership(tmp_path / "membership.json", rows),
        plan_audit=_plan_audit(),
        score_replay_audit=_replay(model_flags),
        **_lifecycle(tmp_path, rows),
    )
    assert next(row for row in resolved if row["ticker"] == "AAA")["eligible_flag"] == 0
    assert audit["eligible_count_by_cohort"] == {"beverages": 2}
    assert audit["no_discretionary_exclusion_pass"] is True


def test_terminal_model_ready_name_stays_in_census_and_tails_are_recomputed(
    tmp_path: Path,
) -> None:
    model_flags = {ticker: 1 for ticker in TICKERS}
    rows = [
        _member(
            ticker,
            lifecycle=("governed_terminal_event" if ticker == "AAA" else "active"),
            model_flag=1,
        )
        for ticker in TICKERS
    ]
    resolved, audit = reconcile_exact_registered_census(
        asof_date=ASOF,
        signals=_signals(model_flags),
        membership_path=_membership(tmp_path / "membership.json", rows),
        plan_audit=_plan_audit(),
        score_replay_audit=_replay(model_flags),
        **_lifecycle(tmp_path, rows),
    )
    index = {row["ticker"]: row for row in resolved}
    assert index["AAA"]["eligible_flag"] == 0
    assert index["AAA"]["selected_top_flag"] == 0
    assert index["BBB"]["selected_top_flag"] == 1
    assert index["CCC"]["selected_bottom_flag"] == 1
    assert audit["registered_candidate_count"] == 3
    assert audit["eligible_count_by_cohort"] == {"beverages": 2}


def test_signed_candidate_ticker_is_not_normalized(tmp_path: Path) -> None:
    candidate_rows = [
        {"ticker": " aaa", "cohort_id": "beverages", "candidate_flag": 1}
    ]
    candidate = {
        "schema_version": CANDIDATE_SCHEMA,
        "rows": candidate_rows,
        "rows_sha256": canonical_sha256(candidate_rows),
    }
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps({"schema_version": UNIVERSE_SCHEMA}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="ticker/cohort census"):
        _candidate_census(candidate_path, universe_path)


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-30 20:00:00+00:00",
        "2026-09-30T15:00:00-05:00",
        "2026-09-30T20:00Z",
    ],
)
def test_consumer_capture_route_rejects_noncanonical_utc(value: str) -> None:
    with pytest.raises(ValueError, match="exact RFC3339 UTC"):
        _consumer_capture_utc(value, label="capture time")


def test_membership_cannot_self_declare_terminal_status(tmp_path: Path) -> None:
    model_flags = {ticker: 1 for ticker in TICKERS}
    asserted_rows = [
        _member(
            ticker,
            lifecycle=("governed_terminal_event" if ticker == "AAA" else "active"),
            model_flag=1,
        )
        for ticker in TICKERS
    ]
    attested_rows = [
        _member(ticker, lifecycle="active", model_flag=1) for ticker in TICKERS
    ]
    with pytest.raises(ValueError, match="differs from independent source"):
        reconcile_exact_registered_census(
            asof_date=ASOF,
            signals=_signals(model_flags),
            membership_path=_membership(
                tmp_path / "membership.json", asserted_rows
            ),
            plan_audit=_plan_audit(),
            score_replay_audit=_replay(model_flags),
            **_lifecycle(tmp_path, attested_rows),
        )

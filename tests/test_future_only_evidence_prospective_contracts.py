from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.prospective_contracts import (
    CAPTURE_RECEIPT_SCHEMA,
    PROSPECTIVE_ROLE,
    RETURN_CONVENTION,
    ProspectiveContract,
    build_strict_capture,
    normalize_signal_rows,
    read_calendar,
    scheduled_asofs,
    validate_strict_capture,
)


class _Authority:
    def verify(self, *_):
        return True

    def verify_snapshot(self, *_):
        return True

    def identity(self):
        return {
            "authority_id": "external-test",
            "public_key_sha256": "a" * 64,
            "algorithm": "Ed25519",
        }


def _contract() -> ProspectiveContract:
    return ProspectiveContract(
        family="test_family",
        policy_id="test_policy_v1",
        effective_from=date(2026, 1, 1),
        first_signal_date=date(2026, 1, 30),
        horizons=(21,),
        minimum_counts={21: 1},
        benchmark_ticker="SPY",
        cadence_id="monthly_true_month_end_v1",
        minimum_ic=0.0,
        minimum_efficacy=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=0.55,
        transaction_cost_bps=20.0,
        top_minus_bottom_basis="net",
    )


def _calendar(path: Path, sessions: list[str]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "session_date",
                "entry_execution_at_utc",
                "exit_execution_at_utc",
                "calendar_id",
                "calendar_provider",
                "calendar_provider_version",
            ],
        )
        writer.writeheader()
        for session in sessions:
            writer.writerow(
                {
                    "session_date": session,
                    "entry_execution_at_utc": f"{session}T14:30:00+00:00",
                    "exit_execution_at_utc": f"{session}T21:00:00+00:00",
                    "calendar_id": "XNYS",
                    "calendar_provider": "exchange_calendars",
                    "calendar_provider_version": "4.13.2",
                }
            )
    return path


def _signals() -> list[dict[str, object]]:
    return [
        {
            "asof_date": "2026-01-30",
            "ticker": "AAA",
            "sleeve_id": "cohort_a",
            "group_id": "cohort_a",
            "score": 1.0,
            "rank": 1,
            "ranking_mode": "ranked",
            "eligible_flag": 1,
            "selected_top_flag": 1,
            "selected_bottom_flag": 0,
        }
    ]


def _capture_case(tmp_path: Path, *, domain_fields=None, use_z: bool = False):
    calendar = _calendar(
        tmp_path / "calendar.csv",
        ["2026-01-30", "2026-02-02", "2026-02-03"],
    )
    rows = normalize_signal_rows(_signals(), asof_date="2026-01-30")
    source_hash = file_sha256(calendar)
    contract = _contract()
    receipt_payload = {
        "schema_version": CAPTURE_RECEIPT_SCHEMA,
        "evidence_role": PROSPECTIVE_ROLE,
        "family": contract.family,
        "policy_id": contract.policy_id,
        "asof_date": "2026-01-30",
        "capture_date": "2026-01-30",
        "source_sha256": {"trading_calendar": source_hash},
        "signal_rows_sha256": canonical_sha256(rows),
        "trading_calendar_sha256": source_hash,
        "horizons": [21],
        "benchmark_ticker": "SPY",
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "cadence_id": contract.cadence_id,
        "return_convention": RETURN_CONVENTION,
        "captured_at_utc": "2026-01-30T22:00:00+00:00",
        "signal_information_cutoff_at_utc": "2026-01-30T21:00:00+00:00",
        "source_max_information_at_utc": "2026-01-30T21:00:00+00:00",
        "source_generated_at_utc": "2026-01-30T21:30:00+00:00",
        "entry_session_date": "2026-02-02",
        "entry_execution_at_utc": "2026-02-02T14:30:00+00:00",
    }
    if use_z:
        for field in (
            "captured_at_utc",
            "signal_information_cutoff_at_utc",
            "source_max_information_at_utc",
            "source_generated_at_utc",
            "entry_execution_at_utc",
        ):
            receipt_payload[field] = str(receipt_payload[field]).replace(
                "+00:00", "Z"
            )
    receipt = tmp_path / "capture_receipt.json"
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    payload = build_strict_capture(
        contract=contract,
        asof_date="2026-01-30",
        signal_rows=_signals(),
        source_paths={"trading_calendar": calendar},
        expected_source_sha256={"trading_calendar": source_hash},
        required_source_roles={"trading_calendar"},
        trading_calendar_path=calendar,
        capture_receipt_path=receipt,
        expected_capture_receipt_sha256=file_sha256(receipt),
        authority=_Authority(),  # type: ignore[arg-type]
        domain_fields=domain_fields,
    )
    return payload, contract, calendar


def test_ticker_is_globally_unique_across_sleeves() -> None:
    rows = _signals() + [{**_signals()[0], "sleeve_id": "cohort_b"}]
    with pytest.raises(ValueError, match="globally unique"):
        normalize_signal_rows(rows, asof_date="2026-01-30")


def test_signal_row_must_explicitly_bind_capture_asof() -> None:
    row = dict(_signals()[0])
    row.pop("asof_date")
    with pytest.raises(ValueError, match="asof_date"):
        normalize_signal_rows([row], asof_date="2026-01-30")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rank", True),
        ("rank", 1.9),
        ("eligible_flag", True),
        ("eligible_flag", 0.9),
    ],
)
def test_signal_integer_fields_reject_bool_and_fractional_values(
    field: str,
    value: object,
) -> None:
    row = dict(_signals()[0])
    row[field] = value
    with pytest.raises(ValueError, match="canonical integer|strict 0/1"):
        normalize_signal_rows([row], asof_date="2026-01-30")


def test_signal_asof_rejects_trailing_text() -> None:
    row = dict(_signals()[0])
    row["asof_date"] = "2026-01-30T00:00:00Z"
    with pytest.raises(ValueError, match="exact YYYY-MM-DD|differs"):
        normalize_signal_rows([row], asof_date="2026-01-30")


@pytest.mark.parametrize("ticker", ["aaa", " AAA", "AAA "])
def test_signal_ticker_rejects_normalized_aliases(ticker: str) -> None:
    row = dict(_signals()[0])
    row["ticker"] = ticker
    with pytest.raises(ValueError, match="canonical uppercase ticker"):
        normalize_signal_rows([row], asof_date="2026-01-30")


def test_capture_asof_and_score_reject_type_coercion() -> None:
    with pytest.raises(ValueError, match="exact YYYY-MM-DD string"):
        normalize_signal_rows(_signals(), asof_date=date(2026, 1, 30))  # type: ignore[arg-type]
    row = dict(_signals()[0])
    row["score"] = "1.0"
    with pytest.raises(ValueError, match="canonical JSON number"):
        normalize_signal_rows([row], asof_date="2026-01-30")


def test_z_timestamp_receipt_round_trips_through_capture_validation(
    tmp_path: Path,
) -> None:
    payload, contract, calendar = _capture_case(tmp_path, use_z=True)
    validated = validate_strict_capture(
        payload,
        contract=contract,
        authority=_Authority(),  # type: ignore[arg-type]
        trading_calendar_path=calendar,
    )
    assert validated["captured_at_utc"].endswith("Z")
    assert validated["trusted_capture_timing"][
        "entry_execution_at_utc"
    ].endswith("Z")


def test_calendar_session_and_registry_cutoff_reject_trailing_text(
    tmp_path: Path,
) -> None:
    bad_calendar = _calendar(tmp_path / "bad-calendar.csv", ["2026-01-30junk"])
    with pytest.raises(ValueError, match="exact YYYY-MM-DD"):
        read_calendar(bad_calendar)
    good_calendar = _calendar(
        tmp_path / "good-calendar.csv",
        ["2026-01-30", "2026-02-02"],
    )
    rows, _ = read_calendar(good_calendar)
    with pytest.raises(ValueError, match="exact YYYY-MM-DD"):
        scheduled_asofs(
            _contract(),
            calendar_rows=rows,
            complete_through_asof="2026-01-30T23:59:59Z",
        )


def test_partial_month_is_not_mislabeled_as_true_month_end(tmp_path: Path) -> None:
    calendar = _calendar(
        tmp_path / "calendar.csv",
        ["2026-01-02", "2026-01-15", "2026-01-30", "2026-02-02"],
    )
    rows, _ = read_calendar(calendar)
    assert scheduled_asofs(
        _contract(),
        calendar_rows=rows,
        complete_through_asof="2026-01-15",
    ) == []
    assert scheduled_asofs(
        _contract(),
        calendar_rows=rows,
        complete_through_asof="2026-01-30",
    ) == ["2026-01-30"]


def test_truncated_terminal_calendar_month_is_not_proven_complete(tmp_path: Path) -> None:
    calendar = _calendar(
        tmp_path / "calendar.csv",
        ["2026-01-02", "2026-01-15", "2026-01-30"],
    )
    rows, _ = read_calendar(calendar)
    assert scheduled_asofs(
        _contract(),
        calendar_rows=rows,
        complete_through_asof="2026-01-30",
    ) == []


def test_domain_fields_cannot_override_canonical_fail_closed_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="collid|reserved|canonical"):
        _capture_case(tmp_path, domain_fields={"optimizer_cap": 1.0})


def test_top_level_capture_timestamp_must_match_signed_timing(tmp_path: Path) -> None:
    payload, contract, calendar = _capture_case(tmp_path)
    payload["captured_at_utc"] = "2026-01-30T20:00:00+00:00"
    payload.pop("payload_sha256")
    payload.pop("capture_id")
    payload["capture_id"] = canonical_sha256(payload)
    payload["payload_sha256"] = canonical_sha256(payload)
    with pytest.raises(ValueError, match="top-level|timing|captured"):
        validate_strict_capture(
            payload,
            contract=contract,
            authority=_Authority(),  # type: ignore[arg-type]
            trading_calendar_path=calendar,
        )


def test_missing_capture_optimizer_cap_is_not_treated_as_zero(tmp_path: Path) -> None:
    payload, contract, calendar = _capture_case(tmp_path)
    payload.pop("optimizer_cap")
    with pytest.raises(ValueError, match="optimizer cap"):
        validate_strict_capture(
            payload,
            contract=contract,
            authority=_Authority(),  # type: ignore[arg-type]
            trading_calendar_path=calendar,
        )

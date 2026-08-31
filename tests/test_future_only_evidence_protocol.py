from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from future_only_evidence.protocol import (
    FutureEvidencePolicy,
    build_capture_payload,
    canonical_sha256,
    evaluate_future_evidence,
    file_sha256,
    immutable_write_json,
    validate_capture_payload,
)


def _write(path: Path, value: object) -> Path:
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _policy(*, counts: int = 2, hit_rate: float = 0.55) -> FutureEvidencePolicy:
    return FutureEvidencePolicy(
        family="test_family",
        policy_id="test_policy_v1",
        effective_from=date(2026, 8, 21),
        first_signal_date=date(2026, 8, 24),
        horizons=(1,),
        minimum_counts={1: counts},
        minimum_ic=0.0,
        minimum_top_minus_cohort=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=hit_rate,
        transaction_cost_bps=20.0,
        minimum_cross_sections={"sleeve_a": 4, "sleeve_b": 4, "group_a": 4, "group_b": 4},
        require_group_pass=True,
        top_minus_bottom_basis="net",
    )


def _signals(asof: str, *, sleeves: tuple[str, ...] = ("sleeve_a",)) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for sleeve_index, sleeve in enumerate(sleeves):
        group = "group_a" if sleeve_index == 0 else "group_b"
        for index, ticker in enumerate(("A", "B", "C", "D"), start=1):
            rows.append(
                {
                    "asof_date": asof,
                    "ticker": f"{ticker}{sleeve_index}",
                    "sleeve_id": sleeve,
                    "group_id": group,
                    "score": float(5 - index),
                    "rank": index,
                    "ranking_mode": "ranked",
                    "eligible_flag": 1,
                    "selected_top_flag": int(index == 1),
                    "selected_bottom_flag": int(index == 4),
                }
            )
    return rows


def _capture(
    tmp_path: Path,
    *,
    asof: str,
    suffix: str,
    sleeves: tuple[str, ...] = ("sleeve_a",),
) -> Path:
    source = _write(tmp_path / f"source_{suffix}.txt", suffix)
    rows = _signals(asof, sleeves=sleeves)
    source_hashes = {"source": file_sha256(source)}
    # Receipt binds the normalized rows.  Inputs are already normalized in this fixture.
    normalized_rows = []
    for row in rows:
        item = dict(row)
        item["ticker"] = str(item["ticker"]).upper()
        item["score"] = float(item["score"])
        item["rank"] = float(item["rank"])
        for flag in ("eligible_flag", "selected_top_flag", "selected_bottom_flag"):
            item[flag] = int(item[flag])
        item["signal_row_sha256"] = canonical_sha256(item)
        normalized_rows.append(item)
    normalized_rows.sort(key=lambda row: (row["sleeve_id"], row["group_id"], row["ticker"]))
    receipt = _write(
        tmp_path / f"receipt_{suffix}.json",
        {
            "schema_version": "future_signal_capture_receipt_v1",
            "family": "test_family",
            "asof_date": asof,
            "capture_date": asof,
            "captured_at_utc": f"{asof}T22:00:00+00:00",
            "source_sha256": source_hashes,
            "signal_rows_sha256": canonical_sha256(normalized_rows),
        },
    )
    payload = build_capture_payload(
        policy=_policy(),
        asof_date=asof,
        capture_date=asof,
        signal_rows=rows,
        source_paths={"source": source},
        expected_source_sha256=source_hashes,
        required_source_roles={"source"},
        trusted_receipt_path=receipt,
        expected_trusted_receipt_sha256=file_sha256(receipt),
        trusted_receipt_verifier=lambda *_: True,
    )
    capture_path = tmp_path / f"capture_{suffix}.json"
    immutable_write_json(capture_path, payload)
    return capture_path


def _outcomes(
    capture_paths: list[Path],
    *,
    intervals: list[tuple[str, str]],
    weak_sleeve: str | None = None,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for capture_path, (entry, exit_date) in zip(capture_paths, intervals):
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        for signal in capture["signal_rows"]:
            if not signal["eligible_flag"]:
                continue
            rank = int(signal["rank"])
            gross_return = {1: 0.10, 2: 0.04, 3: -0.01, 4: -0.05}[rank]
            if signal["sleeve_id"] == weak_sleeve:
                gross_return = -gross_return
            rows.append(
                {
                    "capture_id": capture["capture_id"],
                    "ticker": signal["ticker"],
                    "sleeve_id": signal["sleeve_id"],
                    "group_id": signal["group_id"],
                    "horizon_sessions": 1,
                    "entry_date": entry,
                    "exit_date": exit_date,
                    "gross_return": gross_return,
                    "membership_status": "member_at_entry",
                    "terminal_event_status": "none",
                    "outcome_available_at_utc": f"{exit_date}T23:00:00+00:00",
                }
            )
    return {
        "schema_version": "future_only_outcomes_v1",
        "evidence_class": "prospective_future_only",
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }


def test_capture_rejects_outcome_fields_and_pre_effective_signal(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.txt", "x")
    rows = _signals("2026-08-24")
    rows[0]["forward_return"] = 0.1
    receipt = _write(tmp_path / "receipt.json", {})
    with pytest.raises(ValueError, match="outcome/revealed field"):
        build_capture_payload(
            policy=_policy(),
            asof_date="2026-08-24",
            capture_date="2026-08-24",
            signal_rows=rows,
            source_paths={"source": source},
            expected_source_sha256={"source": file_sha256(source)},
            required_source_roles={"source"},
            trusted_receipt_path=receipt,
            expected_trusted_receipt_sha256=file_sha256(receipt),
            trusted_receipt_verifier=lambda *_: True,
        )
    with pytest.raises(ValueError, match="pre-effective"):
        build_capture_payload(
            policy=_policy(),
            asof_date="2026-08-20",
            capture_date="2026-08-20",
            signal_rows=_signals("2026-08-20"),
            source_paths={"source": source},
            expected_source_sha256={"source": file_sha256(source)},
            required_source_roles={"source"},
            trusted_receipt_path=receipt,
            expected_trusted_receipt_sha256=file_sha256(receipt),
            trusted_receipt_verifier=lambda *_: True,
        )


@pytest.mark.parametrize(
    "asof",
    ["2026-08-24T00:00:00Z", " 2026-08-24", date(2026, 8, 24)],
)
def test_capture_rejects_noncanonical_date_identity(
    tmp_path: Path,
    asof: object,
) -> None:
    source = _write(tmp_path / "source.txt", "x")
    with pytest.raises(ValueError, match="exact YYYY-MM-DD"):
        build_capture_payload(
            policy=_policy(),
            asof_date=asof,  # type: ignore[arg-type]
            capture_date="2026-08-24",
            signal_rows=_signals("2026-08-24"),
            source_paths={"source": source},
            expected_source_sha256={"source": file_sha256(source)},
            required_source_roles={"source"},
            trusted_receipt_path=tmp_path / "unused.json",
            expected_trusted_receipt_sha256="0" * 64,
            trusted_receipt_verifier=lambda *_: True,
        )


def test_evaluation_rejects_noncanonical_utc(tmp_path: Path) -> None:
    capture = _capture(tmp_path, asof="2026-08-24", suffix="one")
    outcome = _write(
        tmp_path / "outcomes.json",
        _outcomes(
            [capture],
            intervals=[("2026-08-25", "2026-08-26")],
        ),
    )
    with pytest.raises(ValueError, match="exact RFC3339 UTC"):
        evaluate_future_evidence(
            policy=_policy(counts=1),
            capture_paths=[capture],
            outcome_path=outcome,
            evaluation_at_utc="2026-08-27 00:00:00+00:00",
        )


def test_immutable_writer_rejects_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "sealed.json"
    immutable_write_json(path, {"x": 1})
    with pytest.raises(FileExistsError, match="already exists"):
        immutable_write_json(path, {"x": 2})


def test_capture_rejects_tampered_payload_hash(tmp_path: Path) -> None:
    path = _capture(tmp_path, asof="2026-08-24", suffix="one")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["signal_rows"][0]["score"] = 999
    with pytest.raises(ValueError, match="payload hash mismatch"):
        validate_capture_payload(payload)


def test_true_nonoverlap_and_initial_final_costs_are_primary(tmp_path: Path) -> None:
    first = _capture(tmp_path, asof="2026-08-24", suffix="one")
    second = _capture(tmp_path, asof="2026-08-25", suffix="two")
    outcome_payload = _outcomes(
        [first, second],
        intervals=[("2026-08-25", "2026-08-29"), ("2026-08-27", "2026-09-02")],
    )
    outcome_path = _write(tmp_path / "outcomes.json", outcome_payload)
    result = evaluate_future_evidence(
        policy=_policy(counts=2),
        capture_paths=[first, second],
        outcome_path=outcome_path,
        evaluation_at_utc="2026-09-03T00:00:00+00:00",
    )
    sleeve = next(row for row in result["scope_verdicts"] if row["scope_kind"] == "sleeve")
    assert sleeve["nonoverlapping_outcome_count"] == 1
    assert sleeve["gates"]["minimum_count_pass"] is False
    assert sleeve["periods"][0]["top_entry_turnover"] == 1.0
    assert sleeve["periods"][-1]["top_exit_turnover"] == 1.0
    assert result["production_activation_authorized"] is False
    assert result["optimizer_cap"] == 0.0


def test_sleeves_are_independent_and_aggregate_cannot_hide_failure(tmp_path: Path) -> None:
    first = _capture(
        tmp_path,
        asof="2026-08-24",
        suffix="one",
        sleeves=("sleeve_a", "sleeve_b"),
    )
    outcome_payload = _outcomes(
        [first],
        intervals=[("2026-08-25", "2026-08-26")],
        weak_sleeve="sleeve_b",
    )
    outcome_path = _write(tmp_path / "outcomes.json", outcome_payload)
    result = evaluate_future_evidence(
        policy=_policy(counts=1),
        capture_paths=[first],
        outcome_path=outcome_path,
        evaluation_at_utc="2026-08-27T00:00:00+00:00",
    )
    verdicts = {row["sleeve_id"]: row for row in result["sleeve_verdicts"]}
    assert verdicts["sleeve_a"]["pass"] is True
    assert verdicts["sleeve_b"]["pass"] is False
    assert result["all_sleeves_pass"] is False
    assert result["action"] == "remain_shadow_fail_closed"


def test_exact_55_percent_hit_threshold_is_not_rounded_down(tmp_path: Path) -> None:
    first = _capture(tmp_path, asof="2026-08-24", suffix="one")
    second = _capture(tmp_path, asof="2026-08-26", suffix="two")
    outcome_payload = _outcomes(
        [first, second],
        intervals=[("2026-08-25", "2026-08-26"), ("2026-08-27", "2026-08-28")],
    )
    # Invert the second period: hit rate becomes exactly 50%.
    second_id = json.loads(second.read_text(encoding="utf-8"))["capture_id"]
    for row in outcome_payload["rows"]:
        if row["capture_id"] == second_id:
            row["gross_return"] = -float(row["gross_return"])
    outcome_payload["rows_sha256"] = canonical_sha256(outcome_payload["rows"])
    outcome_path = _write(tmp_path / "outcomes.json", outcome_payload)
    result = evaluate_future_evidence(
        policy=_policy(counts=2, hit_rate=0.55),
        capture_paths=[first, second],
        outcome_path=outcome_path,
        evaluation_at_utc="2026-08-29T00:00:00+00:00",
    )
    sleeve = next(row for row in result["scope_verdicts"] if row["scope_kind"] == "sleeve")
    assert sleeve["hit_rate"] == 0.5
    assert sleeve["gates"]["hit_rate_pass"] is False

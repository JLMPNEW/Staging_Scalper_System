from __future__ import annotations

import csv
from pathlib import Path

import pytest

from industrials.transportation.future_oos_preflight_v1 import build_operational_preflight
from industrials.transportation.future_oos_protocol_v1 import (
    TRANSPORT_POLICY,
    normalize_signal_rows,
    validate_fresh_sources,
)


def _csv(path: Path, rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _score_rows(asof: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, ticker in enumerate(("A", "B", "C", "D"), start=1):
        rows.append(
            {
                "asof_date": asof,
                "ticker": ticker,
                "calibration_cohort": "oil_tanker_operators_v5",
                "v8_group_id": "oil_tankers",
                "ranking_mode": "ranked",
                "v8_final_score": 100 - index,
                "v8_group_percentile_score": 100 - index,
                "source_rank_ready_flag": 1,
                "group_cross_section_ready_flag": 1,
                "group_specialized_ready_flag": 1,
            }
        )
    return rows


def test_pre_effective_source_is_rejected_before_file_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pre-effective"):
        validate_fresh_sources(
            asof_date="2026-08-20",
            capture_date="2026-08-20",
            score_path=tmp_path / "missing_score.csv",
            rank_path=tmp_path / "missing_rank.csv",
            source_manifest_path=tmp_path / "missing_manifest.json",
        )


def test_stale_current_score_preflight_keeps_clock_fail_closed(tmp_path: Path) -> None:
    score = _csv(tmp_path / "score.csv", _score_rows("2026-08-24"))
    rank = _csv(
        tmp_path / "rank.csv",
        [{"asof_date": "2026-08-24", "ticker": ticker} for ticker in ("A", "B", "C", "D")],
    )
    result = build_operational_preflight(
        preflight_date="2026-08-30",
        score_path=score,
        rank_path=rank,
    )
    assert result["status"] == "clock_not_started"
    assert result["freshness_pass"] is False
    assert result["valid_future_capture_count"] == 0
    assert result["remaining_outcome_count"] == {21: 12, 63: 4}
    assert result["script42_diagnostic_can_authorize"] is False
    assert result["production_activation_authorized"] is False
    assert result["optimizer_cap"] == 0.0
    assert result["earliest_session_math"]["calendar_date_guarantee"] is False


def test_tankers_remain_independently_coverage_gated() -> None:
    rows = _score_rows("2026-08-24")
    rows[0]["group_specialized_ready_flag"] = 0
    signals, coverage = normalize_signal_rows(rows)
    assert len(signals) == 4
    assert coverage == {"oil_tanker_operators_v5": False}
    assert TRANSPORT_POLICY.minimum_counts == {21: 12, 63: 4}
    assert TRANSPORT_POLICY.minimum_hit_rate == 0.55


def test_pre_first_signal_does_not_start_clock(tmp_path: Path) -> None:
    score = _csv(tmp_path / "score.csv", _score_rows("2026-08-21"))
    rank = _csv(
        tmp_path / "rank.csv",
        [{"asof_date": "2026-08-21", "ticker": ticker} for ticker in ("A", "B", "C", "D")],
    )
    result = build_operational_preflight(
        preflight_date="2026-08-21",
        score_path=score,
        rank_path=rank,
    )
    assert result["clock_started"] is False
    assert result["status"] == "clock_not_started"
    assert any("pre-first-signal" in blocker for blocker in result["blockers"])

from __future__ import annotations

import json
from pathlib import Path

import pytest

from consumer_defensive.core.future_oos_capture_v4 import derive_rank_signals
from future_only_evidence.protocol import canonical_sha256


def _snapshot(path: Path, *, role: str = "prospective_future_only_capture", bad_rank=False) -> Path:
    rows = [
        {
            "asof_date": "2026-08-31",
            "ticker": ticker,
            "sleeve_id": "beverages",
            "group_id": "beverages",
            "score": score,
            "rank": rank,
            "ranking_mode": "ranked",
            "eligible_flag": 1,
            "selected_top_flag": int(rank == 1),
            "selected_bottom_flag": int(rank == 2),
        }
        for ticker, score, rank in (
            ("AAA", 90.0, 2 if bad_rank else 1),
            ("BBB", 10.0, 1 if bad_rank else 2),
        )
    ]
    payload = {
        "schema_version": "consumer_defensive_future_rank_snapshot_v1",
        "evidence_role": role,
        "asof_date": "2026-08-31",
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_rank_snapshot_is_the_only_signal_source(tmp_path: Path) -> None:
    rows = derive_rank_signals(
        _snapshot(tmp_path / "rank.json"),
        asof_date="2026-08-31",
    )
    assert [(row["ticker"], row["rank"]) for row in rows] == [("AAA", 1), ("BBB", 2)]


def test_rank_score_order_mismatch_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rank order"):
        derive_rank_signals(
            _snapshot(tmp_path / "rank.json", bad_rank=True),
            asof_date="2026-08-31",
        )


def test_unknown_or_diagnostic_evidence_role_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not prospective"):
        derive_rank_signals(
            _snapshot(tmp_path / "rank.json", role="foo"),
            asof_date="2026-08-31",
        )

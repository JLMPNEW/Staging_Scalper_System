from __future__ import annotations

import csv
from pathlib import Path

from industrials.transportation.future_oos_preflight_v3 import build_operational_preflight


def _csv(path: Path) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asof_date", "ticker"])
        writer.writeheader()
        writer.writerow({"asof_date": "2026-07-30", "ticker": "AAA"})
    return path


def test_missed_transport_signal_is_not_backfilled(tmp_path: Path) -> None:
    payload = build_operational_preflight(
        preflight_date="2026-08-25",
        score_path=_csv(tmp_path / "score.csv"),
        rank_path=_csv(tmp_path / "rank.csv"),
    )
    assert payload["clock_started"] is False
    assert payload["status"] == "clock_not_started"
    assert payload["missed_original_signal_can_be_backfilled"] is False
    assert payload["remaining_nonoverlapping_observations_per_sleeve"] == {"21": 12, "63": 4}
    assert payload["calendar_date_guarantee"] is False

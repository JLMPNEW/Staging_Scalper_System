from __future__ import annotations

import csv
from pathlib import Path

from industrials.transportation.future_oos_preflight_v2 import build_operational_preflight


def _write(path: Path, asof: str) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asof_date", "ticker"])
        writer.writeheader()
        writer.writerow({"asof_date": asof, "ticker": "A"})
    return path


def test_pre_effective_rows_are_explicitly_diagnostic(tmp_path: Path) -> None:
    result = build_operational_preflight(
        preflight_date="2026-08-25",
        score_path=_write(tmp_path / "score.csv", "2026-07-30"),
        rank_path=_write(tmp_path / "rank.csv", "2026-07-30"),
    )
    assert result["clock_started"] is False
    assert result["latest_score_policy_effective_pass"] is False
    assert result["current_artifact_classification"] == "pre_effective_historical_diagnostic"
    assert any("pre-policy-effective" in blocker for blocker in result["blockers"])

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from future_only_evidence.protocol import file_sha256
from future_only_evidence.protocol_v2 import evaluate_future_evidence_v2


def _calendar(path: Path) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["session_date"])
        writer.writeheader()
        for value in ("2026-08-24", "2026-08-25", "2026-08-26"):
            writer.writerow({"session_date": value})
    return path


def test_claimed_horizon_must_match_bound_calendar_before_evaluation(
    tmp_path: Path,
) -> None:
    calendar = _calendar(tmp_path / "calendar.csv")
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "source_identities": {
                    "trading_calendar": {"sha256": file_sha256(calendar)}
                }
            }
        ),
        encoding="utf-8",
    )
    outcome = tmp_path / "outcome.json"
    outcome.write_text(
        json.dumps(
            {
                "trading_calendar_sha256": file_sha256(calendar),
                "rows": [
                    {
                        "entry_date": "2026-08-24",
                        "exit_date": "2026-08-26",
                        "horizon_sessions": 1,
                        "entry_session_index": 0,
                        "exit_session_index": 2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="claimed trading sessions"):
        evaluate_future_evidence_v2(
            policy=None,  # type: ignore[arg-type]
            capture_paths=[capture],
            outcome_path=outcome,
            trading_calendar_path=calendar,
            evaluation_at_utc="2026-08-27T00:00:00+00:00",
        )


def test_tampered_calendar_hash_is_rejected_before_evaluation(tmp_path: Path) -> None:
    calendar = _calendar(tmp_path / "calendar.csv")
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"source_identities": {}}), encoding="utf-8")
    outcome = tmp_path / "outcome.json"
    outcome.write_text(
        json.dumps({"trading_calendar_sha256": "0" * 64, "rows": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exact trading calendar"):
        evaluate_future_evidence_v2(
            policy=None,  # type: ignore[arg-type]
            capture_paths=[capture],
            outcome_path=outcome,
            trading_calendar_path=calendar,
            evaluation_at_utc="2026-08-27T00:00:00+00:00",
        )

"""Trading-calendar-bound future evidence evaluation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Sequence

from .protocol import (
    FutureEvidencePolicy,
    canonical_sha256,
    evaluate_future_evidence,
    exact_sha256,
    file_sha256,
)


def _calendar(path: Path) -> tuple[list[str], dict[str, int]]:
    resolved = Path(path).expanduser().resolve()
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("trading calendar is empty")
    date_field = "session_date" if "session_date" in rows[0] else "date"
    sessions = [str(row.get(date_field) or "")[:10] for row in rows]
    if any(not value for value in sessions) or sessions != sorted(set(sessions)):
        raise ValueError("trading calendar sessions must be unique and strictly sorted")
    return sessions, {value: index for index, value in enumerate(sessions)}


def evaluate_future_evidence_v2(
    *,
    policy: FutureEvidencePolicy,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    trading_calendar_path: Path,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    """Verify exact session horizons before applying the v1 economic gates."""

    calendar_hash = file_sha256(trading_calendar_path)
    _, session_index = _calendar(trading_calendar_path)
    outcome_payload = json.loads(Path(outcome_path).read_text(encoding="utf-8"))
    if outcome_payload.get("trading_calendar_sha256") != calendar_hash:
        raise ValueError("outcomes do not bind the exact trading calendar bytes")
    for capture_path in capture_paths:
        capture = json.loads(Path(capture_path).read_text(encoding="utf-8"))
        identity = capture.get("source_identities", {}).get("trading_calendar")
        if not isinstance(identity, dict) or identity.get("sha256") != calendar_hash:
            raise ValueError("capture does not bind the same exact trading calendar")
    interval_indexes: dict[tuple[str, str], tuple[int, int]] = {}
    for row in outcome_payload.get("rows", []):
        entry = str(row.get("entry_date") or "")[:10]
        exit_date = str(row.get("exit_date") or "")[:10]
        if entry not in session_index or exit_date not in session_index:
            raise ValueError("outcome interval date is absent from the bound trading calendar")
        entry_index = session_index[entry]
        exit_index = session_index[exit_date]
        horizon = int(row.get("horizon_sessions") or 0)
        if exit_index - entry_index != horizon:
            raise ValueError("outcome interval does not span the claimed trading sessions")
        supplied_entry = int(row.get("entry_session_index", -1))
        supplied_exit = int(row.get("exit_session_index", -1))
        if (supplied_entry, supplied_exit) != (entry_index, exit_index):
            raise ValueError("outcome session indexes do not match the bound calendar")
        key = (entry, exit_date)
        interval_indexes.setdefault(key, (entry_index, exit_index))
        if interval_indexes[key] != (entry_index, exit_index):
            raise ValueError("outcome interval index identity is inconsistent")
    result = evaluate_future_evidence(
        policy=policy,
        capture_paths=capture_paths,
        outcome_path=outcome_path,
        evaluation_at_utc=evaluation_at_utc,
    )
    result.pop("payload_sha256", None)
    result.update(
        schema_version="future_only_evidence_evaluation_v2",
        trading_calendar={
            "path": str(Path(trading_calendar_path).expanduser().resolve()),
            "sha256": exact_sha256(calendar_hash),
            "session_count": len(session_index),
        },
        exact_session_horizon_pass=True,
    )
    result["payload_sha256"] = canonical_sha256(result)
    return result


__all__ = ["evaluate_future_evidence_v2"]

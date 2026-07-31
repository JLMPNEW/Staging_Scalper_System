from __future__ import annotations

import json
from pathlib import Path

import pytest

from industrials.machinery.conditional_promotion_v14 import (
    DEFAULT_PROTOCOL_PATH,
    _begin_open_event,
    _gate_row,
    build_conditional_stage12_candidate,
    conditional_paths,
    load_conditional_protocol,
    open_conditional_lockbox,
)
from industrials.machinery.scoring import file_sha256


def test_conditional_protocol_preserves_v13_and_live_cap() -> None:
    protocol = load_conditional_protocol()
    assert protocol["candidate_id"] == "equal_components"
    assert protocol["decision_policy"]["confidence_bound_role"] == (
        "advisory_only"
    )
    assert protocol["decision_policy"]["stage8_v1_3_result_is_not_overridden"]
    assert protocol["portfolio_contract"]["maximum_provisional_cap"] == 0.05
    assert protocol["decision_policy"]["production_cap_increase_permitted"] is False


def test_lockbox_open_requires_token_before_writing_event(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="token"):
        open_conditional_lockbox(
            {},
            approval_token="WRONG",
            output_root=tmp_path,
        )
    assert not conditional_paths(tmp_path).open_event.exists()


def test_open_event_is_exclusive_and_never_stores_plain_token(
    tmp_path: Path,
) -> None:
    path = tmp_path / "open.json"
    token = "OPEN_ONCE"
    first = _begin_open_event(
        path=path,
        protocol_sha256="protocol",
        freeze_sha256="freeze",
        token=token,
    )
    second = _begin_open_event(
        path=path,
        protocol_sha256="protocol",
        freeze_sha256="freeze",
        token=token,
    )
    assert first == second
    assert first["lockbox_spent"] is True
    assert token not in path.read_text(encoding="utf-8")
    with pytest.raises(PermissionError, match="resume token"):
        _begin_open_event(
            path=path,
            protocol_sha256="protocol",
            freeze_sha256="freeze",
            token="ANOTHER",
        )


def test_gate_comparisons_are_inclusive_at_threshold() -> None:
    minimum = _gate_row(
        "minimum",
        actual=0.0,
        threshold=0.0,
        direction="minimum",
    )
    maximum = _gate_row(
        "maximum",
        actual=0.75,
        threshold=0.75,
        direction="maximum",
    )
    assert minimum["status"] == "PASS"
    assert maximum["status"] == "PASS"


def test_blocked_lockbox_cannot_build_stage12_candidate(
    tmp_path: Path,
) -> None:
    paths = conditional_paths(tmp_path)
    paths.acceptance_json.parent.mkdir(parents=True)
    paths.acceptance_json.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "conditional_promotion_status": "BLOCKED_KEEP_ACTIVE_MODEL",
                "lockbox_outcomes_accessed": True,
                "stage8_v1_3_result_overridden": False,
            }
        ),
        encoding="utf-8",
    )
    paths.run_manifest_json.write_text(
        json.dumps(
            {
                "protocol_definition_sha256": file_sha256(
                    DEFAULT_PROTOCOL_PATH
                ),
                "files": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hard gates did not pass"):
        build_conditional_stage12_candidate(
            {},
            asof="2026-07-28",
            output_root=tmp_path,
        )

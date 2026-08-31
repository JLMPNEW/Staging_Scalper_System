from __future__ import annotations

import pytest

from future_only_evidence.production_activation import (
    _change_control_authority,
    build_activation_registry,
    effective_scope_locks,
)


def _lock(scope: str, effective_from: str, marker: str) -> dict[str, object]:
    return {
        "family": "transportation",
        "scope_id": scope,
        "effective_from": effective_from,
        "effective_to": "",
        "payload_sha256": marker * 64,
    }


def test_registry_is_deterministically_sorted_and_effective_dated() -> None:
    earlier = _lock("airlines", "2026-09-01", "a")
    later = _lock("airlines", "2026-10-01", "b")
    retail = _lock("railroads", "2026-09-01", "c")
    registry = build_activation_registry(
        [later, retail, earlier],
        family="transportation",
        generated_at_utc="2026-08-31T20:00:00+00:00",
    )
    assert [
        (row["scope_id"], row["effective_from"]) for row in registry["locks"]
    ] == [
        ("airlines", "2026-09-01"),
        ("airlines", "2026-10-01"),
        ("railroads", "2026-09-01"),
    ]
    selected = effective_scope_locks(registry, asof_date="2026-10-15")
    assert selected["airlines"]["payload_sha256"] == "b" * 64
    assert selected["railroads"]["payload_sha256"] == "c" * 64


def test_unconfigured_change_control_root_fails_closed(tmp_path) -> None:
    key = tmp_path / "unused.pem"
    key.write_text("not configured", encoding="utf-8")
    with pytest.raises(ValueError, match="change-control authority is unconfigured"):
        _change_control_authority(
            "transportation",
            public_key_path=key,
        )

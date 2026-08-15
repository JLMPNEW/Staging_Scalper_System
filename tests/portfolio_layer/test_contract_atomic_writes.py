from __future__ import annotations

import json
import os
from pathlib import Path

from portfolio_layer.core import contracts


def test_write_manifest_retries_transient_permission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "manifest.json"
    real_replace = os.replace
    calls = 0

    def flaky_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("transient sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(contracts.os, "replace", flaky_replace)

    contracts.write_manifest(target, {"acceptance": "PASS"})

    assert calls == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "acceptance": "PASS"

    }

def test_write_manifest_survives_extended_onedrive_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "manifest.json"
    real_replace = os.replace
    calls = 0

    def locked_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls <= 10:
            raise PermissionError("extended OneDrive sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(contracts.os, "replace", locked_replace)
    monkeypatch.setattr(contracts.time, "sleep", lambda _seconds: None)

    contracts.write_manifest(target, {"acceptance": "PASS"})

    assert calls == 11
    assert json.loads(target.read_text(encoding="utf-8"))["acceptance"] == "PASS"


def test_atomic_replace_does_not_retry_non_sharing_os_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "manifest.json"
    calls = 0

    def invalid_replace(_source: str | Path, _destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        raise OSError("non-sharing filesystem error")

    monkeypatch.setattr(contracts.os, "replace", invalid_replace)

    try:
        contracts.write_manifest(target, {"acceptance": "PASS"})
    except OSError as exc:
        assert "non-sharing" in str(exc)
    else:
        raise AssertionError("non-sharing OSError unexpectedly retried or suppressed")
    assert calls == 1

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

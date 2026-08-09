from __future__ import annotations

import os

from portfolio_layer.core.runtime_env import hydrate_missing_user_environment


def test_hydrate_missing_user_environment_never_overrides(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEEP_ME", "process-value")
    monkeypatch.delenv("ADD_ME", raising=False)
    seen: list[str] = []

    def reader(name: str) -> str | None:
        seen.append(name)
        return {
            "KEEP_ME": "user-value",
            "ADD_ME": "new-value",
            "MISSING": None,
        }.get(name)

    hydrated = hydrate_missing_user_environment(
        ["KEEP_ME", "ADD_ME", "MISSING"],
        reader=reader,
    )

    assert hydrated == ("ADD_ME",)
    assert os.environ["KEEP_ME"] == "process-value"
    assert os.environ["ADD_ME"] == "new-value"
    assert seen == ["ADD_ME", "MISSING"]


def test_hydrate_missing_user_environment_ignores_blank_names(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ADD_ME", raising=False)

    hydrated = hydrate_missing_user_environment(
        ["", " ", "ADD_ME"],
        reader=lambda name: "value" if name == "ADD_ME" else None,
    )

    assert hydrated == ("ADD_ME",)

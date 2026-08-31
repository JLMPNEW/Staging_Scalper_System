from __future__ import annotations

import json

import pytest

from consumer_defensive.core.promotion_artifacts_v3 import publish_immutable_json


def test_immutable_json_publish_is_atomic_idempotent_and_divergence_safe(
    tmp_path,
) -> None:
    destination = tmp_path / "review" / "decision.json"
    payload = {"schema_version": "test_v3", "value": 1}

    publish_immutable_json(destination, payload)
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    publish_immutable_json(destination, payload)

    with pytest.raises(FileExistsError, match="divergent"):
        publish_immutable_json(destination, {**payload, "value": 2})


def test_immutable_json_publish_rejects_nonfinite_values_without_output(
    tmp_path,
) -> None:
    destination = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        publish_immutable_json(destination, {"value": float("nan")})
    assert not destination.exists()

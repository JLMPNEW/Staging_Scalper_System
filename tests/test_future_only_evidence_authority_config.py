from __future__ import annotations

from pathlib import Path

import pytest

from future_only_evidence.authority_config import (
    DEFAULT_AUTHORITY_REGISTRY,
    load_pinned_authority,
)


def test_default_unconfigured_registry_stops_future_clock(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unconfigured"):
        load_pinned_authority(
            "transportation",
            public_key_path=tmp_path / "attacker_generated_key.pem",
            registry_path=DEFAULT_AUTHORITY_REGISTRY,
        )

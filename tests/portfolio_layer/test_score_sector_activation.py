from __future__ import annotations

import pytest

from portfolio_layer.core.config import active_score_sectors, score_sector_is_active


def test_date_effective_sector_activation_is_inclusive() -> None:
    sector = {
        "model_family": "consumer_defensive",
        "enabled": True,
        "enabled_from": "2026-08-28",
        "enabled_until": "2026-10-29",
    }

    assert not score_sector_is_active(sector, "2026-08-27")
    assert score_sector_is_active(sector, "2026-08-28")
    assert score_sector_is_active(sector, "2026-10-29")
    assert not score_sector_is_active(sector, "2026-10-30")
    assert score_sector_is_active(sector, None)


def test_active_score_sectors_preserves_pre_activation_history() -> None:
    config = {
        "score_contract": {
            "sectors": [
                {"model_family": "biotech", "enabled": True},
                {
                    "model_family": "consumer_defensive",
                    "enabled": True,
                    "enabled_from": "2026-08-28",
                },
            ]
        }
    }

    assert [s["model_family"] for s in active_score_sectors(config, "2026-08-27")] == ["biotech"]
    assert [s["model_family"] for s in active_score_sectors(config, "2026-08-28")] == [
        "biotech",
        "consumer_defensive",
    ]


def test_invalid_activation_window_fails_closed() -> None:
    sector = {
        "enabled": True,
        "enabled_from": "2026-08-29",
        "enabled_until": "2026-08-28",
    }
    with pytest.raises(ValueError, match="enabled_from"):
        score_sector_is_active(sector, "2026-08-28")


def test_invalid_sector_config_shape_fails_closed() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        active_score_sectors({"score_contract": {"sectors": {}}}, "2026-08-28")

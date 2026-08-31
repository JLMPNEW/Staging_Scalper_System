from consumer_defensive.core.future_oos_capture_v2 import (
    REQUIRED_CAPTURE_ROLES_V2,
    REQUIRED_FRESHNESS_GATES,
)


def test_consumer_capture_requires_calendar_and_all_freshness_gates() -> None:
    assert "trading_calendar" in REQUIRED_CAPTURE_ROLES_V2
    assert REQUIRED_FRESHNESS_GATES == {
        "market_data_fresh",
        "financial_data_fresh",
        "positioning_data_fresh",
        "membership_current",
        "terminal_events_current",
    }

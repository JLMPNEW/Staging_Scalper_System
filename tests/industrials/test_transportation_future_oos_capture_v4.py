from industrials.transportation.future_oos_capture_v4 import REQUIRED_CAPTURE_ROLES_V4


def test_calendar_is_a_required_capture_identity() -> None:
    assert "trading_calendar" in REQUIRED_CAPTURE_ROLES_V4
    assert len(REQUIRED_CAPTURE_ROLES_V4) == 7

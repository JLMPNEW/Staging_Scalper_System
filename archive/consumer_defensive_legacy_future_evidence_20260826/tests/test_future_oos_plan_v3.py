from consumer_defensive.core.future_oos_plan_v3 import (
    CANONICAL_TARGET_CONTRACT,
    CANONICAL_THRESHOLDS,
    REQUIRED_PLAN_ROLES_V3,
)


def test_consumer_target_is_explicit_fixed_session_xlp_residual() -> None:
    assert CANONICAL_TARGET_CONTRACT["target_field"] == "forward_xlp_residual_return"
    assert CANONICAL_TARGET_CONTRACT["target_horizons_sessions"] == [21, 63, 126]
    assert CANONICAL_TARGET_CONTRACT["exit_policy"] == "fixed_trading_session_horizons_21_63_126"
    assert CANONICAL_THRESHOLDS["minimum_top_xlp_residual_net"] == 0.0
    assert "trading_calendar" in REQUIRED_PLAN_ROLES_V3

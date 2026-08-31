from __future__ import annotations

import inspect

from industrials.transportation import future_oos_protocol_v2


def test_equal_weight_groups_are_explicit_na_not_vacuous_pass() -> None:
    source = inspect.getsource(future_oos_protocol_v2.evaluate)
    assert '"applicability": "not_applicable"' in source
    assert '"pass": None' in source
    assert '"not_applicable_excluded_from_group_pass_denominator": True' in source

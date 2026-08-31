from __future__ import annotations

import inspect

from industrials.transportation import future_oos_capture_v3


def test_domain_capture_preserves_shared_evaluator_schema() -> None:
    source = inspect.getsource(future_oos_capture_v3.capture_signal)
    assert 'domain_schema_version="transportation_future_only_signal_capture_v3"' in source
    assert 'payload.update(\n        schema_version=' not in source

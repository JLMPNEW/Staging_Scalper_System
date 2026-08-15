from __future__ import annotations

import importlib.util
from pathlib import Path

from industrials.transportation.dedicated_parser_adapter import ADAPTER_VERSION


def test_tanker_coverage_audit_tracks_the_live_adapter_contract() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "industrials"
        / "transportation"
        / "scripts"
        / "36f_audit_transportation_tanker_parser_coverage.py"
    )
    spec = importlib.util.spec_from_file_location("tanker_coverage_audit", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.ADAPTER_VERSION == ADAPTER_VERSION

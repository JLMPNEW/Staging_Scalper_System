from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "24_run_transportation_release_acceptance.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "transportation_release_acceptance_script",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_acceptance_commands_are_read_only_code_gates() -> None:
    commands = _module().acceptance_commands()
    assert [gate for gate, _ in commands] == [
        "full_industrials_and_dedicated_parser_tests",
        "transportation_ruff",
        "transportation_pyright",
        "transportation_compile",
    ]
    flattened = [token for _, command in commands for token in command]
    assert "pytest" in flattened
    assert "ruff" in flattened
    assert "pyright" in flattened
    assert "compileall" in flattened
    prohibited = {
        "09j_run_transportation_efficient_parser_batch.py",
        "19_build_transportation_pit_feature_history.py",
        "19h_run_transportation_bounded_walk_forward_calibration.py",
        "03_sync_transportation_prices.py",
    }
    assert prohibited.isdisjoint(flattened)
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "machinery"
    / "scripts"
    / "17_run_machinery_refresh_pipeline.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "machinery_refresh_runtime_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dedicated_parser_python_resolves_config_and_cli_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    configured = tmp_path / "configured-python.exe"
    override = tmp_path / "override-python.exe"
    configured.touch()
    override.touch()
    monkeypatch.setenv("MACHINERY_TEST_PARSER_PYTHON", str(configured))
    config = {
        "dedicated_parser": {
            "python_executable": "${MACHINERY_TEST_PARSER_PYTHON}",
        }
    }

    assert module.resolve_dedicated_parser_python(
        cli_value=None,
        config=config,
        base_dir=tmp_path,
    ) == configured.resolve()
    assert module.resolve_dedicated_parser_python(
        cli_value=override,
        config=config,
        base_dir=tmp_path,
    ) == override.resolve()


def test_dedicated_parser_dependency_probe_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    executable = tmp_path / "python.exe"
    executable.touch()

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="missing parser package",
        ),
    )
    with pytest.raises(RuntimeError, match="missing parser package"):
        module.validate_dedicated_parser_python(executable)

